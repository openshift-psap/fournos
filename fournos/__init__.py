from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("fournos")
except PackageNotFoundError:
    __version__ = "dev"
