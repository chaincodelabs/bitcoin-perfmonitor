import socket
import os
import multiprocessing
import re
import tempfile
from typing import Optional as Op
import typing as t
from enum import Enum
from pathlib import Path

import yaml
from pydantic import BaseModel, validator, PositiveInt
from pydantic.dataclasses import dataclass

from . import logging, util

logger = logging.get_logger()

# Get physical memory specs
MEM_GIB = (
    os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES') / (1024. ** 3))

DEFAULT_NPROC = min(4, int(multiprocessing.cpu_count()))

HOSTNAME = socket.gethostname()
BENCH_NAMES = {
    'gitclone', 'build', 'makecheck', 'functionaltests',
    'microbench', 'ibd', 'reindex'}

# TODO shoulddn't be creating files on import
config_path = Path.home() / '.bitcoinperf'
config_path.mkdir(exist_ok=True)

# Where the synced peer optionally resides.
peer_path = config_path / 'peer'
peer_repo = peer_path / 'bitcoin'
peer_datadir = peer_path / 'datadir'
base_datadirs = config_path / 'base_datadirs'
pruned_500k_datadir = base_datadirs / 'pruned-500k'


class Compilers(str, Enum):
    clang = 'clang'
    gcc = 'gcc'


@dataclass
class GitCheckout:
    # e.g. "HEAD"
    ref: str
    remote: str
    # e.g. "e59c59c7befdbb0a600b557f05f009c03f98c2c8"
    sha: str
    # Used to verify cache correctness later on.
    commit_msg: str
    # Human-readable name.
    name: str
    # If this was rebased (based on Target.rebase), then note the original
    # sha.
    pre_rebase_sha: Op[str] = None


def is_valid_path(p: str):
    return Path(os.path.expandvars(p))


def is_writeable_path(p: str):
    if not os.access(Path(p).parent, os.W_OK):
        raise ValueError("path {} is not writable".format(p))
    return Path(p)


def is_datadir(path: Path):
    if not ((path / 'blocks').exists() and (path / 'chainstate').exists()):
        raise ValueError("path isn't a valid datadir")
    return path


def path_exists(path: Path):
    if not path.exists():
        raise ValueError("path doesn't exist")
    return path


def is_built_bitcoin(path: Path):
    if not ((path / 'src' / 'bitcoind').exists() and
            (path / 'src' / 'bitcoin-cli').exists()):
        raise ValueError("path doesn't have bitcoin binaries")
    return path


def is_compiler(name):
    if name not in list(Compilers):
        raise ValueError("compiler not recognized")
    return name


def is_port_open(addr: str) -> str:
    hostname, port = addr, '8333'
    if ':' in addr:
        hostname, port = addr.split(':')

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.connect((hostname, int(port)))
        s.shutdown(2)
        return addr
    except Exception:
        raise ValueError("can't connect to node at {}".format(addr))


class NodeAddr(str):
    """An address:port string pointing to a running bitcoin node."""
    @classmethod
    def __get_validators__(cls):
        yield is_port_open


def _expandvars(s: str):
    if isinstance(s, str):
        return os.path.expandvars(s)
    return s


class EnvStr(str):
    @classmethod
    def __get_validators__(cls):
        yield _expandvars


class ExistingDatadir(Path):
    @classmethod
    def __get_validators__(cls):
        yield is_valid_path
        yield path_exists
        yield is_datadir


class RepoDir(Path):
    @classmethod
    def __get_validators__(cls):
        yield is_valid_path
        yield path_exists


class WriteablePath(Path):
    @classmethod
    def __get_validators__(cls):
        yield is_writeable_path


class SyncedPeer(BaseModel):
    datadir: ExistingDatadir = ''
    repodir: RepoDir = ''
    bitcoind_extra_args: str = ''
    # or
    address: Op[NodeAddr] = None

    # The ref that will be checked out on this peer.
    gitref: Op[str] = '0.20'

    # TODO actually use this
    def validate_either_or(self, data):
        if not (set(data.keys()).issuperset({'datadir', 'repodir'}) or
                'address' in data):
            raise ValueError("synced_peer config not valid")

    def __hash__(self):
        return hash(str(self.datadir) + str(self.repodir) +
                    str(self.address) + str(self.gitref))


def get_envname():
    return {
        'bench-odroid-1': 'ccl-bench-odroid-1',
        'bench-raspi-1': 'ccl-bench-raspi-1',
        'bench-hdd-1': 'ccl-bench-hdd-1',
        'bench-ssd-1': 'ccl-bench-ssd-1',
        'bench-ssd-6': 'ccl-bench-ssd-6',
    }.get(HOSTNAME, '')


class Codespeed(BaseModel):
    url: EnvStr
    username: EnvStr
    password: EnvStr
    envname: Op[EnvStr] = None

    @validator('envname', always=True)
    def infer_envname(cls, v):
        return v or get_envname()


class Bench(BaseModel):
    enabled: bool = True
    run_count: PositiveInt = 1


class BenchBuild(Bench):
    num_jobs: Op[PositiveInt] = DEFAULT_NPROC
    configure_args: EnvStr = ""


class BenchUnittests(Bench):
    num_jobs: Op[PositiveInt] = DEFAULT_NPROC


class BenchFunctests(Bench):
    num_jobs: Op[PositiveInt] = DEFAULT_NPROC


class BenchMicrobench(Bench):
    filter: str = ''


class BenchIbdFromNetwork(Bench):
    start_height: PositiveInt = 0
    end_height: Op[PositiveInt] = None
    time_heights: Op[t.List[PositiveInt]] = None
    stash_datadir: Op[WriteablePath] = None


class BenchIbdFromLocal(Bench):
    start_height: PositiveInt = 0
    end_height: Op[PositiveInt] = None
    time_heights: Op[t.List[PositiveInt]] = None
    stash_datadir: Op[WriteablePath] = None


class BenchIbdRangeFromLocal(Bench):
    src_datadir: ExistingDatadir
    start_height: PositiveInt = 0
    end_height: Op[PositiveInt]
    time_heights: Op[t.List[PositiveInt]] = None


class BenchReindex(Bench):
    # TODO:
    # If None, we'll use the resulting datadir from the previous benchmark.
    src_datadir: Op[Path] = None
    start_height: PositiveInt = 0
    end_height: PositiveInt = None
    time_heights: Op[t.List[PositiveInt]] = None
    stash_datadir: Op[WriteablePath] = None


class BenchReindexChainstate(Bench):
    # TODO:
    # If None, we'll use the resulting datadir from the previous benchmark.
    src_datadir: Op[Path] = None
    start_height: PositiveInt = 0
    end_height: PositiveInt = None
    time_heights: Op[t.List[PositiveInt]] = None
    stash_datadir: Op[WriteablePath] = None


class Benches(BaseModel):
    build: Op[BenchBuild] = None
    unittests: Op[BenchUnittests] = None
    functests: Op[BenchFunctests] = None
    microbench: Op[BenchMicrobench] = None
    ibd_from_network: Op[BenchIbdFromNetwork] = None
    ibd_from_local: Op[BenchIbdFromLocal] = None
    ibd_range_from_local: Op[BenchIbdRangeFromLocal] = None
    reindex: Op[BenchReindex] = None
    reindex_chainstate: Op[BenchReindexChainstate] = None


class Target(BaseModel):
    """
    Data that uniquely identifies a bitcoin configuration to benchmark.
    """
    gitref: EnvStr
    gitremote: EnvStr = "origin"
    bitcoind_extra_args: EnvStr = ""
    configure_args: EnvStr = ""

    # Used for display in output.
    name: Op[EnvStr] = ""

    # If True, rebase this branch on top of latest master.
    rebase: bool = True

    # Set when and if the gitref is successfully resolved to a particular
    # commit.
    gitco: Op[GitCheckout] = None

    @property
    def cache_key(self):
        """
        A unique, shortish identifier suitable for use as an ID in a cache.

        TODO unittest this
        """
        sha = util.sha256(self._hash_str)
        ref = re.sub('[^0-9a-zA-Z]', '-', self.gitref[:16])
        return f'{ref}-{sha[:16]}'

    @property
    def id(self):
        """A short, human-readable ID."""
        return "{}-{}".format(
            self.gitref,
            re.sub(r'\s+', '', self.bitcoind_extra_args).replace('-', ''))

    @validator('name', always=True)
    def make_name(cls, v, values, **kwargs):
        if not v:
            return values['gitref']
        return v

    @property
    def _hash_str(self):
        return (
            self.gitref + self.gitremote + self.bitcoind_extra_args +
            self.name + self.configure_args + str(self.rebase))

    def __hash__(self):
        return hash(self._hash_str)


class Slack(BaseModel):
    webhook_url: Op[EnvStr] = None


class Config(BaseModel):
    to_bench: t.List[Target]

    workdir: Op[Path] = None
    synced_peer: Op[SyncedPeer] = None
    compilers: t.List[Compilers] = [Compilers.clang, Compilers.gcc]
    slack: Op[Slack] = None
    log_level: str = 'INFO'
    teardown: bool = True
    safety_checks: bool = True
    clean: bool = True
    cache_build: bool = False
    cache_git: bool = False
    cache_build_size: int = 3
    codespeed: Op[Codespeed] = None
    benches: Op[Benches] = None

    @validator('workdir', pre=True, always=True)
    def mk_workdir(cls, v):
        if not v:
            return Path(tempfile.mkdtemp(prefix='bitcoinperf-'))
        return Path(v)

    @validator('benches', whole=True)
    def check_peer(cls, v, values, **kwargs):
        if v.ibd_from_local or v.ibd_range_from_local:
            if not values.get('synced_peer'):
                raise ValueError(
                    "synced_peer must be specified when running "
                    "IBD- or reindex-based benchmarks")

        return v

    def bitcoinperf_home_path(self):
        return config_path

    def build_cache_path(self):
        p = self.bitcoinperf_home_path() / 'build-cache'
        p.mkdir(exist_ok=True, parents=True)
        return p

    @property
    def results_dir(self):
        d = self.workdir / 'results'
        d.mkdir(exist_ok=True)
        return d


def load(content: t.Union[Path, str]) -> Config:
    if isinstance(content, Path):
        content = content.read_text()

    return Config(**yaml.load(content), Loader=yaml.Loader)
