#!/usr/bin/python3
# -*- coding: utf-8; tab-width: 4; indent-tabs-mode: t -*-

import os
import copy
import json
from gi.repository import GLib


class Db:

    def __init__(self):
        selfDir = os.path.dirname(os.path.realpath(__file__))

        self.dictOfficial = None
        with open(os.path.join(selfDir, "db-official.json")) as f:
            self.dictOfficial = json.load(f)

        self.dictExtended = copy.deepcopy(self.dictOfficial)
        with open(os.path.join(selfDir, "db-extended.json")) as f:
            self.dictExtended.update(json.load(f))

    def get(self, extended=False):
        if not extended:
            return self.dictOfficial
        else:
            return self.dictExtended

    def query(self, country=None, location=None, protocolList=None, extended=False, maximum=1):
        assert location is None or (country is not None and location is not None)
        assert protocolList is None or all(x in ["http", "ftp", "rsync"] for x in protocolList)

        # select database
        srcDict = self.dictOfficial if not extended else self.dictExtended

        # country out of scope, we don't consider this condition
        if country is not None:
            if not any(x.get("country", None) == country for x in srcDict.values()):
                country = None
                location = None

        # location out of scope, same as above
        if location is not None:
            if not any(x["country"] == country and x.get("location", None) == location for x in srcDict.values()):
                location = None

        # do query
        ret = []
        for url, prop in srcDict.items():
            if len(ret) >= maximum:
                break
            if country is not None and prop.get("country", None) != country:
                continue
            if location is not None and prop.get("location", None) != location:
                continue
            if protocolList is not None and prop.get("protocol", None) not in protocolList:
                continue
            ret.append(url)
        return ret


class PortageDb:

    def __init__(self):
        selfDir = os.path.dirname(os.path.realpath(__file__))

        self.dictOfficial = None
        with open(os.path.join(selfDir, "db-official.json")) as f:
            self.dictOfficial = self._convertDict(json.load(f))

        self.dictExtended = copy.deepcopy(self.dictOfficial)
        with open(os.path.join(selfDir, "db-extended.json")) as f:
            self.dictExtended.update(self._convertDict(json.load(f)))

    def get(self, extended=False):
        if not extended:
            return self.dictOfficial
        else:
            return self.dictExtended

    def query(self, country=None, location=None, protocolList=None, extended=False, maximum=1):
        assert location is None or (country is not None and location is not None)
        assert protocolList is None or protocolList == ["rsync"]

        # select database
        srcDict = self.dictOfficial if not extended else self.dictExtended

        # country out of scope, we don't consider this condition
        if country is not None:
            if not any(x.get("country", None) == country for x in srcDict.values()):
                country = None
                location = None

        # location out of scope, same as above
        if location is not None:
            if not any(x["country"] == country and x.get("location", None) == location for x in srcDict.values()):
                location = None

        # do query
        ret = []
        for url, prop in srcDict.items():
            if len(ret) >= maximum:
                break
            if country is not None and prop.get("country", None) != country:
                continue
            if location is not None and prop.get("location", None) != location:
                continue
            ret.append(url)
        return ret

    def _convertDict(self, srcDict):
        ret = dict()
        for url, prop in srcDict.items():
            if prop["protocol"] != "rsync":
                continue
            url = self._convertUrl(url)
            if url is None:
                continue
            ret[url] = prop
        return ret

    def _convertUrl(self, srcUrl):
        url = srcUrl.rstrip("/")
        if not url.endswith("/gentoo"):
            return None
        url += "-portage"
        return url


class Updater:

    def __init__(self, api, gentooOrGentooPortage=True):
        self.api = api
        self.proc = None
        if gentooOrGentooPortage:
            self.db = Db()
        else:
            self.db = PortageDb()

    def init_start(self):
        assert self.proc is None
        self._start(None)

    def init_stop(self):
        self.proc.terminate()

    def update_start(self, schedDatetime):
        assert self.proc is None
        self._start(schedDatetime)

    def update_stop(self):
        self.proc.terminate()

    def _start(self, schedDatetime):
        source = self.db.query(self.api.get_country(), self.api.get_location(), ["rsync"], True)[0]
        dataDir = self.api.get_data_dir()
        if schedDatetime is None:
            logFile = os.path.join(self.api.get_log_dir(), "rsync-init.log")
        else:
            logFile = os.path.join(self.api.get_log_dir(), "rsync-%s.log" % (schedDatetime))
        cmd = "/usr/bin/rsync -a -z --delete \"%s\" \"%s\"" % (source, dataDir)
        self.proc = _ShellProc(cmd, self._finishCallback)

    def _finishCallback(self):
        self.api.notify_progress(100, True)
        self.proc = None


class PortageUpdater(Updater):

    def __init__(self, api):
        super().__init__(api, False)


class _ShellProc:

    def __init__(self, cmd, exitCallback):
        targc, targv = GLib.shell_parse_argv(cmd)
        flags = GLib.SpawnFlags.DO_NOT_REAP_CHILD | GLib.SpawnFlags.CHILD_INHERITS_STDIN | GLib.SpawnFlags.STDOUT_TO_DEV_NULL | GLib.SpawnFlags.STDERR_TO_DEV_NULL
        ret = GLib.spawn_async_with_fds(None,                                           # working_directory
                                        targv,                                          # argv
                                        None,                                           # envp
                                        flags,                                          # flags
                                        None,                                           # child_setup
                                        None,                                           # user_data
                                        -1,                                             # stdin_fd
                                        -1,                                             # stdout_fd
                                        -1)                                             # stderr_fd
        if not ret[0]:
            raise Exception("failed to create process")
        self.pid = ret[1]
        self.exitCallback = exitCallback
        self.pidWatch = GLib.child_watch_add(self.pid, self._exitCallback)

    def terminate(self):
        # FIXME
        pass

    def _exitCallback(self, dummy1, dummy2):        # FIXME
        self.exitCallback()
        GLib.source_remove(self.pidWatch)
        self.pidWatch = None
        GLib.spawn_close_pid(self.pid)
        self.pid = None
