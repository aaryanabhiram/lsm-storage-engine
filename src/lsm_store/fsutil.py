import os


def fsync_dir(path):
    # TODO windows: nothing happens here, a crash right after a rename can lose the new name even though
    # the contents were fsynced. only really safe on linux/mac
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
