import sys

from .application import ArchivistApplication

def main(version):
    app = ArchivistApplication()
    return app.run(sys.argv)
