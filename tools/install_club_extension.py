"""Install the scoped extension into a build context, never into a live DB."""
import argparse
from pathlib import Path
import shutil


def install(root):
    target = Path(root) / "src" / "license_service"
    handler = target / "http_server.py"
    source = handler.read_text()
    anchor = "    def do_POST(self) -> None:  # noqa: N802\n"
    injection = '''        if self.path == "/internal/club-vip":
            from .club_vip import handle
            handle(self)
            return
'''
    if 'from .club_vip import handle' not in source:
        if source.count(anchor) != 1:
            raise RuntimeError("Unsupported service adapter")
        handler.write_text(source.replace(anchor,anchor+injection))
    shutil.copyfile(Path(__file__).resolve().parents[1]/"server_extension"/"club_vip.py",target/"club_vip.py")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service_root")
    install(parser.parse_args().service_root)
