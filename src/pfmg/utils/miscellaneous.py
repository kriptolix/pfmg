import shutil

def is_available() -> bool:
        """Return True if flatpak is available on the host."""
        return shutil.which("flatpak") is not None