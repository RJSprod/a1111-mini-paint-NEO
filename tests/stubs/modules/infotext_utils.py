"""The two names the Canvas reads from the host's paste machinery."""

from PIL import Image

paste_fields: dict = {}


def image_from_url_text(filedata):
    if filedata is None:
        return None
    if isinstance(filedata, list):
        if not filedata:
            return None
        filedata = filedata[0]
    if isinstance(filedata, tuple) and len(filedata) == 2:
        return filedata[0]
    if isinstance(filedata, Image.Image):
        return filedata
    return None
