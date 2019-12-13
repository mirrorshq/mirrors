#!/usr/bin/python3
# -*- coding: utf-8; tab-width: 4; indent-tabs-mode: t -*-

import os


class McConst:

    etcDir = "/etc/mirrors"
    libDir = "/usr/lib64/mirrors"
    pluginsDir = os.path.join(libDir, "plugins")
    libexecDir = "/usr/libexec/mirrors"
    updaterExe = os.path.join(libexecDir, "updater_proc.py")
    cacheDir = "/var/cache/mirrors"
    runDir = "/run/mirrors"
    logDir = "/var/log/mirrors"


class McParam:

    def __init__(self):
        self.tmpDir = "/tmp/mirrors"

        self.cfg = None

        self.pluginList = []
        self.publicMirrorDatabaseList = []
        self.mirrorSiteList = []

        self.listenIp = "0.0.0.0"

        self.apiPort = 2300
        self.httpPort = 80      # can be "random"
        self.ftpPort = 21       # can be "random"
        self.rsyncPort = 1001   # can be "random"       # FIXME

        self.avahiSupport = True

        # objects
        self.mainloop = None
        self.pluginManager = None
        self.apiServer = None
        self.avahiObj = None
        self.updater = None
        self.advertiser = None
