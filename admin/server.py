#!/usr/bin/env python3
"""Admin console for the QuakeJS server.

Drives the running dedicated server by writing to the console FIFO the entrypoint
supervises, installs maps from lvlworld or an upload, and follows the game log
for chat, a leaderboard and the live roster. The work lives in the qadmin
package beside this file; see its docstring for the layout, and
qadmin/config.py for the environment it reads.
"""

from qadmin.web import main

if __name__ == "__main__":
    main()
