from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("fournos")
except PackageNotFoundError:
    __version__ = "dev"
