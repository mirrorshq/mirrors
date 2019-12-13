#!/usr/bin/python3
# -*- coding: utf-8; tab-width: 4; indent-tabs-mode: t -*-

import sys
import json
from datetime import datetime
sys.path.append('/usr/lib64/mirrors')
from mc_util import McUtil
from mc_util import DynObject
from mc_param import McConst
from mc_plugin import McPublicMirrorDatabase


class Main:

    def __init__(self):
        self.tmpDir = sys.stdin.readline()

        self.mirrorSiteId = sys.stdin.readline()
        self.mirrorSiteDataDir = sys.stdin.readline()

        self.db = None
        if True:
            bHasPublicMirrorDatabase = (sys.stdin.readline() == "1")
            if bHasPublicMirrorDatabase:
                jsonOfficial = sys.stdin.readline()
                jsonExtended = sys.stdin.readline()
                self.db = McPublicMirrorDatabase.createFromJson(self.mirrorSiteId, jsonOfficial, jsonExtended)

        self.realUpdaterObj = None
        if True:
            filename = sys.stdin.readline()
            classname = sys.stdin.readline()
            self.realUpdaterObj = McUtil.loadObject(filename, classname)

        self.api = None
        if True:
            bInitOrUpdate = (sys.stdin.readline() == "1")
            if bInitOrUpdate:
                self.api = self._createInitOrUpdateApi()
            else:
                schedDatetime = datetime.strptime(sys.stdin.readline(), "%Y-%m-%d %H:%M")
                self.api = self._createInitOrUpdateApi(schedDatetime)

    def run(self):
        try:
            self.realUpdaterObj.run(self.api)
            if self.api is not None:
                self.api.progress_changed(100)
        except:
            if self.api is not None:
                self.api.error_occured(sys.exc_info())

    def progressCallback(self, progress):
        sys.stdout.write(json.dumps(progress))
        sys.stdout.write("\n")

    def errorCallback(self, exc_info):
        # FIXME
        pass

    def errorAndHoldForCallback(self, seconds, exc_info):
        # FIXME
        pass

    def _createInitOrUpdateApi(self, schedDatetime=None):
        api = DynObject()
        api.get_country = lambda: "CN"
        api.get_location = lambda: None
        api.get_data_dir = lambda: self.mirrorSiteDataDir
        api.get_log_dir = lambda: McConst.logDir
        api.get_public_mirror_database = lambda: self.db

        if schedDatetime is not None:
            # means update api
            api.get_sched_datetime = lambda: schedDatetime

        api.progress_changed = self.progressCallback
        api.error_occured = self.errorCallback
        api.error_occured_and_hold_for = self.errorAndHoldForCallback

        return api


###############################################################################

if __name__ == "__main__":
    Main().run()
