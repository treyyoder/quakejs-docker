"""The QuakeJS admin console, as a package.

server.py is the entry point. Each module here owns one concern:

    config    environment, paths and limits shared by more than one module
    auth      credentials, sessions, the sign-in lockout, and who a request is from
    follow    the one file follower both log tailers use
    game      the game server itself: its console FIFO, its log, status
    settings  the settings the console manages - the one spec build-config.py
              reads too - and the saved settings and rotation
    assets    paks, maps, bots, and installs from lvlworld or an upload
    chat      the message stream, public chat and its throttle
    stats     the engine's own game log: leaderboard, arrivals, live roster
    web       the HTTP layer: request plumbing and dispatch
    routes    every endpoint, one function each

Everything written to the game console is executed as a server command, so
every value that reaches it is validated against a whitelist rather than
escaped. That rule holds in whichever module the value passes through.
"""
