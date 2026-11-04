"""The home folder and the settings file.

Everything Fuseshot keeps lives under one home folder: the photo cache, the
lists it writes, the work folders, downloaded models and the settings file
fuseshot.toml. It is --home, else FUSESHOT_HOME, else the usual per-user data
folder (for example ~/.local/share/fuseshot or %LOCALAPPDATA%\\fuseshot).
"""

import os
from pathlib import Path

SETTINGS_NAME = "fuseshot.toml"
KNOWN_KEYS = ("output", "sources", "outputs", "upload", "tools", "models", "jobs", "max_megapixels", "crop")
OUTPUTS = ("panoramas", "splats", "loops", "groups", "wigglegrams", "composites", "enhance")
# Results that are only made once turned on.
OFF_BY_DEFAULT = ("enhance",)
_home = None
_settings = None


class SettingsError(RuntimeError):
    pass


def default_home():
    env = os.environ.get("FUSESHOT_HOME")
    if env:
        return Path(env).expanduser()
    from platformdirs import user_data_path

    return user_data_path("fuseshot", appauthor=False)


def setup(home=None, settings_file=None):
    """Pick the home folder and read the settings file; call once at start."""
    global _home, _settings
    _home = Path(home).expanduser().resolve() if home else default_home().resolve()
    os.environ["FUSESHOT_HOME"] = str(_home)
    if settings_file and not Path(settings_file).expanduser().is_file():
        raise SettingsError(f"no settings file at {settings_file}")
    _settings = load(Path(settings_file).expanduser() if settings_file else settings_path())
    return _home


def home():
    return _home or default_home()


def crop_default():
    """Whether a kept panorama is exported cut to the largest clean rectangle unless
    the uncropped view was picked for it. crop = false in the settings file turns it off."""
    value = settings().get("crop", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "no", "off", "0")
    return bool(value)


def unknown_keys(found=None):
    """Top-level names in the settings file that Fuseshot does not read, usually typos."""
    found = settings() if found is None else found
    return [key for key in found if key not in KNOWN_KEYS]


def home_problem(folder=None):
    """Why the home folder cannot be written to, or None. A folder that does not exist
    yet is fine as long as it can be made."""
    folder = Path(folder or home())
    probe = folder
    while not probe.exists():
        if probe.parent == probe:
            break
        probe = probe.parent
    if not probe.is_dir():
        return f"the home folder {folder} is under {probe}, which is not a folder"
    if not os.access(probe, os.W_OK | os.X_OK):
        where = "" if probe == folder else f" ({probe} cannot be written to, so it cannot be made)"
        return (f"cannot write to the home folder {folder}{where}; pick another with --home or "
                "FUSESHOT_HOME, or fix its permissions")
    return None


def settings_path():
    return home() / SETTINGS_NAME


def load(path):
    if not path or not Path(path).exists():
        return {}
    import tomllib

    with open(path, "rb") as f:
        try:
            return tomllib.load(f)
        except tomllib.TOMLDecodeError as err:
            raise SettingsError(f"{path}: {err}") from None


def settings():
    global _settings
    if _settings is None:
        _settings = load(settings_path())
    return _settings


def reload():
    """Read the settings file again, after the settings page has written it."""
    global _settings
    _settings = load(settings_path())
    return _settings


def wants(output):
    """Whether a kind of result is to be made. All are, unless [outputs] in the settings
    file turns one off (panoramas = false); those in OFF_BY_DEFAULT need turning on."""
    if output not in OUTPUTS:
        raise KeyError(output)
    value = section("outputs").get(output)
    return value is True if output in OFF_BY_DEFAULT else value is not False


def section(name):
    value = settings().get(name)
    return value if isinstance(value, dict) else {}


def path(*parts):
    return home().joinpath(*parts)
