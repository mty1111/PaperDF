"""Run offline tests with a stack dump if a GUI/native call stalls the suite."""
import faulthandler
from pathlib import Path
import sys
import unittest


def main():
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    faulthandler.enable()
    faulthandler.dump_traceback_later(120, exit=True)
    try:
        suite = unittest.defaultTestLoader.discover(str(root / 'tests'))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1
    finally:
        faulthandler.cancel_dump_traceback_later()


if __name__ == '__main__':
    sys.exit(main())
