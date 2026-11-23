"""What the macOS app starts. PyInstaller turns this file into Perplexify.app/Contents/MacOS/Perplexify.

With no arguments it opens the app window, like `python -m perplexify`; with arguments it is the command line program
(`Perplexify selftest`, `Perplexify serve --no-browser`), which is how the build checks the finished bundle.
"""
import multiprocessing
import sys

if __name__ == "__main__":
    # Indexing parses files in worker processes. A frozen app starts them by running this same executable again, and this
    # call is what makes that copy behave as a worker instead of opening a second window.
    multiprocessing.freeze_support()
    from perplexify.__main__ import _main

    sys.exit(_main())
