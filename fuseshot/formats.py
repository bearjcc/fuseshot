"""Teach Pillow the photo formats phones use beyond JPEG: HEIC and, if installed, JPEG XL."""

_done = False


def register():
    global _done
    if _done:
        return
    _done = True
    try:
        from pi_heif import register_heif_opener
    except ImportError:
        try:
            from pillow_heif import register_heif_opener
        except ImportError:
            register_heif_opener = None
    if register_heif_opener:
        register_heif_opener()
    try:
        import pillow_jxl  # noqa: F401  registers the JPEG XL opener on import
    except ImportError:
        pass


def heif_ready():
    register()
    from PIL import Image

    return ".heic" in Image.registered_extensions()
