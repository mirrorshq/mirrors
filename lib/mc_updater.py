#!/usr/bin/python3
# -*- coding: utf-8; tab-width: 4; indent-tabs-mode: t -*-

import os
import re
import math
import json
import fcntl
import logging
import subprocess
from datetime import datetime
from croniter import croniter
from collections import OrderedDict
from gi.repository import GLib
from mc_util import McUtil
from mc_util import RotatingFile
from mc_util import GLibIdleInvoker
from mc_util import UnixDomainSocketApiServer
from mc_param import McConst


class McMirrorSiteUpdater:

    MIRROR_SITE_UPDATE_STATUS_INIT = 0
    MIRROR_SITE_UPDATE_STATUS_INITING = 1
    MIRROR_SITE_UPDATE_STATUS_INIT_FAIL = 2
    MIRROR_SITE_UPDATE_STATUS_IDLE = 3
    MIRROR_SITE_UPDATE_STATUS_UPDATING = 4
    MIRROR_SITE_UPDATE_STATUS_UPDATE_FAIL = 5

    MIRROR_SITE_RESTART_INTERVAL = 60

    def __init__(self, param):
        self.param = param

        self.invoker = GLibIdleInvoker()
        self.scheduler = _Scheduler()

        self.updaterDict = dict()                                       # dict<mirror-id,updater-object>
        for ms in self.param.mirrorSiteDict.values():
            self.updaterDict[ms.id] = _OneMirrorSiteUpdater(self, ms)

        self.apiServer = _ApiServer(self)

    def dispose(self):
        self.apiServer.dispose()
        for updater in self.updaterDict.values():
            if updater.status == self.MIRROR_SITE_UPDATE_STATUS_INITING:
                updater.initStop()
            elif updater.status == self.MIRROR_SITE_UPDATE_STATUS_UPDATING:
                updater.updateStop()
        # FIXME, should use g_main_context_iteration to wait all the updaters to stop
        self.scheduler.dispose()
        self.invoker.dispose()

    def isMirrorSiteInitialized(self, mirrorSiteId):
        ret = self.updaterDict[mirrorSiteId].status
        if self.MIRROR_SITE_UPDATE_STATUS_INIT <= ret <= self.MIRROR_SITE_UPDATE_STATUS_INIT_FAIL:
            return False
        return True

    def getMirrorSiteUpdateState(self, mirrorSiteId):
        updater = self.updaterDict[mirrorSiteId]
        ret = dict()
        ret["update_status"] = updater.status
        ret["last_update_time"] = updater.lastUpdateDatetime
        if self.updater.status in [self.MIRROR_SITE_UPDATE_STATUS_INITING, self.MIRROR_SITE_UPDATE_STATUS_UPDATING]:
            ret["update_progress"] = updater.progress
        return ret


class _OneMirrorSiteUpdater:

    def __init__(self, parent, mirrorSite):
        self.param = parent.param
        self.invoker = parent.invoker
        self.scheduler = parent.scheduler
        self.mirrorSite = mirrorSite

        # state files
        self.initFlagFile = os.path.join(self.mirrorSite.masterDir, "INITIALIZED")
        self.lastUpdateDatetimeFile = os.path.join(self.mirrorSite.masterDir, "LAST_UPDATE_DATETIME")

        bInit = True
        if self.__isInitialized():
            bInit = False
        if self.mirrorSite.initializerExe is None:
            bInit = False

        if bInit:
            self.status = McMirrorSiteUpdater.MIRROR_SITE_UPDATE_STATUS_INIT
            self.lastUpdateDatetime = None
        else:
            self.status = McMirrorSiteUpdater.MIRROR_SITE_UPDATE_STATUS_IDLE
            self.lastUpdateDatetime = self.__readLastUpdateDatetime()

        if bInit:
            self.invoker.add(self.initStart)
        else:
            self._postInit()

    def initStart(self):
        assert self.status in [McMirrorSiteUpdater.MIRROR_SITE_UPDATE_STATUS_INIT, McMirrorSiteUpdater.MIRROR_SITE_UPDATE_STATUS_INIT_FAIL]

        try:
            self.status = McMirrorSiteUpdater.MIRROR_SITE_UPDATE_STATUS_INITING
            self._createVars("init")
            self.proc = self._createProc("init")
            self.pidWatch = GLib.child_watch_add(self.proc.pid, self.initExitCallback)
            self.stdoutWatch = GLib.io_add_watch(self.proc.stdout, GLib.IO_IN, self.stdoutCallback)
            self.logger = RotatingFile(os.path.join(McConst.logDir, "%s.log" % (self.mirrorSite.id)), McConst.updaterLogFileSize, McConst.updaterLogFileCount)
            logging.info("Mirror site \"%s\" initialization starts." % (self.mirrorSite.id))
        except Exception:
            self._clearVars("init")
            self.status = McMirrorSiteUpdater.MIRROR_SITE_UPDATE_STATUS_INIT_FAIL
            logging.error("Mirror site \"%s\" initialization failed, re-initialize in %d seconds." % (self.mirrorSite.id, McMirrorSiteUpdater.MIRROR_SITE_RESTART_INTERVAL), exc_info=True)
            self.reInitHandler = GLib.timeout_add_seconds(McMirrorSiteUpdater.MIRROR_SITE_RESTART_INTERVAL, self._reInitCallback)

    def initStop(self):
        assert self.status == McMirrorSiteUpdater.MIRROR_SITE_UPDATE_STATUS_INITING
        self.proc.terminate()

    def initProgressCallback(self, progress):
        assert self.status == McMirrorSiteUpdater.MIRROR_SITE_UPDATE_STATUS_INITING
        if progress > self.progress:
            self.progress = progress
            logging.info("Mirror site \"%s\" initialization progress %d%%." % (self.mirrorSite.id, self.progress))
        elif progress == self.progress:
            pass
        else:
            raise Exception("invalid progress")

    def initErrorCallback(self, exc_info):
        assert self.status == McMirrorSiteUpdater.MIRROR_SITE_UPDATE_STATUS_INITING
        assert self.excInfo is None
        self.excInfo = exc_info

    def initErrorAndHoldForCallback(self, seconds, exc_info):
        assert self.status == McMirrorSiteUpdater.MIRROR_SITE_UPDATE_STATUS_INITING
        assert self.excInfo is None
        self.excInfo = exc_info
        self.holdFor = seconds

    def initExitCallback(self, pid, status):
        assert self.status == McMirrorSiteUpdater.MIRROR_SITE_UPDATE_STATUS_INITING

        try:
            GLib.spawn_check_exit_status(status)
            # child process returns ok
            self.__setInitialized()
            self._clearVars("init")
            self.status = McMirrorSiteUpdater.MIRROR_SITE_UPDATE_STATUS_IDLE
            logging.info("Mirror site \"%s\" initialization finished." % (self.mirrorSite.id))
            self._postInit()
        except GLib.Error as e:
            # child process returns failure
            holdFor = self.holdFor
            self._clearVars("init")
            self.status = McMirrorSiteUpdater.MIRROR_SITE_UPDATE_STATUS_INIT_FAIL
            if holdFor is None:
                logging.error("Mirror site \"%s\" initialization failed (code: %d), re-initialize in %d seconds." % (self.mirrorSite.id, e.code, McMirrorSiteUpdater.MIRROR_SITE_RESTART_INTERVAL))
                self.reInitHandler = GLib.timeout_add_seconds(McMirrorSiteUpdater.MIRROR_SITE_RESTART_INTERVAL, self._reInitCallback)
            else:
                logging.error("Mirror site \"%s\" initialization failed (code: %d), hold for %d seconds before re-initialization." % (self.mirrorSite.id, e.code, holdFor))
                self.reInitHandler = GLib.timeout_add_seconds(holdFor, self._reInitCallback)

    def updateStart(self, schedDatetime):
        assert self.status in [McMirrorSiteUpdater.MIRROR_SITE_UPDATE_STATUS_IDLE, McMirrorSiteUpdater.MIRROR_SITE_UPDATE_STATUS_UPDATING, McMirrorSiteUpdater.MIRROR_SITE_UPDATE_STATUS_UPDATE_FAIL]

        if self.status == McMirrorSiteUpdater.MIRROR_SITE_UPDATE_STATUS_UPDATING:
            logging.info("Mirror site \"%s\" updating ignored on \"%s\", last update is not finished." % (self.mirrorSite.id, schedDatetime.strftime("%Y-%m-%d %H:%M")))
            return

        try:
            self.status = McMirrorSiteUpdater.MIRROR_SITE_UPDATE_STATUS_UPDATING
            self._createVars("update")
            self.schedDatetime = schedDatetime
            self.proc = self._createProc("update")
            self.pidWatch = GLib.child_watch_add(self.proc.pid, self.updateExitCallback)
            self.stdoutWatch = GLib.io_add_watch(self.proc.stdout, GLib.IO_IN, self.stdoutCallback)
            self.logger = RotatingFile(os.path.join(McConst.logDir, "%s.log" % (self.mirrorSite.id)), McConst.updaterLogFileSize, McConst.updaterLogFileCount)
            logging.info("Mirror site \"%s\" update triggered on \"%s\"." % (self.mirrorSite.id, self.schedDatetime.strftime("%Y-%m-%d %H:%M")))
        except Exception:
            self._clearVars("update")
            self.status = McMirrorSiteUpdater.MIRROR_SITE_UPDATE_STATUS_UPDATE_FAIL
            logging.error("Mirror site \"%s\" update failed." % (self.mirrorSite.id), exc_info=True)

    def updateStop(self):
        assert self.status == McMirrorSiteUpdater.MIRROR_SITE_UPDATE_STATUS_UPDATING
        self.proc.terminate()

    def updateProgressCallback(self, progress):
        assert self.status == McMirrorSiteUpdater.MIRROR_SITE_UPDATE_STATUS_UPDATING
        if progress > self.progress:
            self.progress = progress
            logging.info("Mirror site \"%s\" update progress %d%%." % (self.mirrorSite.id, self.progress))
        elif progress == self.progress:
            pass
        else:
            raise Exception("invalid progress")

    def updateErrorCallback(self, exc_info):
        assert self.status == McMirrorSiteUpdater.MIRROR_SITE_UPDATE_STATUS_UPDATING
        assert self.excInfo is None
        self.excInfo = exc_info

    def updateErrorAndHoldForCallback(self, seconds, exc_info):
        assert self.status == McMirrorSiteUpdater.MIRROR_SITE_UPDATE_STATUS_UPDATING
        assert self.excInfo is None
        self.excInfo = exc_info
        self.holdFor = seconds

    def updateExitCallback(self, pid, status):
        assert self.status == McMirrorSiteUpdater.MIRROR_SITE_UPDATE_STATUS_UPDATING
        try:
            GLib.spawn_check_exit_status(status)
            # child process returns ok
            self.__writeLastUpdateDatetime(self.schedDatetime)
            self._clearVars("update")
            self.status = McMirrorSiteUpdater.MIRROR_SITE_UPDATE_STATUS_IDLE
            logging.info("Mirror site \"%s\" update finished." % (self.mirrorSite.id))
        except GLib.Error as e:
            # child process returns failure
            holdFor = self.holdFor
            self._clearVars("update")
            self.status = McMirrorSiteUpdater.MIRROR_SITE_UPDATE_STATUS_UPDATE_FAIL
            if holdFor is None:
                logging.error("Mirror site \"%s\" update failed (code: %d)." % (self.mirrorSite.id, e.code))
            else:
                # is there really any effect since the period is always hours?
                self.scheduler.pauseJob(self.mirrorSite.id, datetime.now() + datetime.timedelta(seconds=holdFor))
                logging.error("Mirror site \"%s\" updates failed (code: %d), hold for %d seconds." % (self.mirrorSite.id, e.code, holdFor))

    def maintainStart(self):
        assert self.status == McMirrorSiteUpdater.MIRROR_SITE_UPDATE_STATUS_IDLE
        try:
            self._createVars("maintain")
            self.proc = self._createProc("maintain")
            self.pidWatch = GLib.child_watch_add(self.proc.pid, self.maintainExitCallback)
            self.stdoutWatch = GLib.io_add_watch(self.proc.stdout, GLib.IO_IN, self.stdoutCallback)
            self.logger = RotatingFile(os.path.join(McConst.logDir, "%s.log" % (self.mirrorSite.id)), McConst.updaterLogFileSize, McConst.updaterLogFileCount)
            logging.info("Mirror site \"%s\" maintainer started.")
        except Exception:
            self._clearVars("maintainer")
            logging.error("Mirror site \"%s\" maintainer start failed, restart it in %d seconds." % (self.mirrorSite.id, McMirrorSiteUpdater.MIRROR_SITE_RESTART_INTERVAL), exc_info=True)
            self.reMaintainHandler = GLib.timeout_add_seconds(McMirrorSiteUpdater.MIRROR_SITE_RESTART_INTERVAL, self._reMaintainCallback)

    def maintainStop(self):
        assert self.status == McMirrorSiteUpdater.MIRROR_SITE_UPDATE_STATUS_IDLE
        self.proc.terminate()

    def maintainErrorCallback(self, exc_info):
        assert self.status == McMirrorSiteUpdater.MIRROR_SITE_UPDATE_STATUS_IDLE
        assert self.excInfo is None
        self.excInfo = exc_info

    def maintainExitCallback(self, pid, status):
        assert self.status == McMirrorSiteUpdater.MIRROR_SITE_UPDATE_STATUS_IDLE

        code = self.proc.returncode
        self._clearVars("maintain")
        logging.error("Mirror site \"%s\" maintainer exited (code: %d), restart it in %d seconds." % (self.mirrorSite.id, code, McMirrorSiteUpdater.MIRROR_SITE_RESTART_INTERVAL))
        self.reMaintainHandler = GLib.timeout_add_seconds(McMirrorSiteUpdater.MIRROR_SITE_RESTART_INTERVAL, self._reMaintainCallback)

    def stdoutCallback(self, source, cb_condition):
        try:
            self.logger.write(source.read())
        finally:
            return True

    def _createVars(self, jtype):
        self.proc = None
        self.pidWatch = None
        self.stdoutWatch = None
        self.logger = None
        self.excInfo = None

        if jtype == "init":
            self.progress = 0
            self.holdFor = None
        elif jtype == "update":
            self.schedDatetime = None
            self.progress = 0
            self.holdFor = None
        elif jtype == "maintain":
            pass
        else:
            assert False

    def _clearVars(self, jtype):
        if jtype == "init":
            del self.holdFor
            del self.progress
        elif jtype == "update":
            del self.schedDatetime
            del self.holdFor
            del self.progress
        elif jtype == "maintain":
            pass
        else:
            assert False

        del self.excInfo
        if self.logger is not None:
            self.logger.close()
        del self.loger
        if self.stdoutWatch is not None:
            GLib.source_remove(self.stdoutWatch)
        del self.stdoutWatch
        if self.pidWatch is not None:
            GLib.source_remove(self.pidWatch)
        del self.pidWatch
        if self.proc is not None and self.proc.returncode is not None:
            self.proc.terminate()
            self.proc.wait()
        del self.proc

    def _createProc(self, jtype):
        cmd = []

        # create log directory
        logDir = os.path.join(McConst.logDir, self.mirrorSite.id)
        McUtil.ensureDir(logDir)

        # executable
        if jtype == "init":
            cmd.append(self.mirrorSite.initializerExe)
        elif jtype == "update":
            cmd.append(self.mirrorSite.updaterExe)
        elif jtype == "maintain":
            cmd.append(self.mirrorSite.maintainerExe)
        else:
            assert False

        # argument
        if True:
            args = {
                "id": self.mirrorSite.id,
                "config": self.mirrorSite.cfgDict,
                "state-directory": self.mirrorSite.pluginStateDir,
                "log-directory": logDir,
                "debug-flag": "",
                "country": self.param.mainCfg["country"],
                "location": self.param.mainCfg["location"],
                "run-mode": jtype,
            }
            for storageName, storageObj in self.mirrorSite.storageDict.items():
                args["storage-" + storageName] = storageObj.pluginParam
            if jtype == "update":
                args["sched-datetime"] = datetime.strftime(self.schedDatetime, "%Y-%m-%d %H:%M")
        cmd.append(json.dumps(args))

        # create process
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
        fcntl.fcntl(proc.stdout, fcntl.F_SETFL, fcntl.fcntl(proc.stdout, fcntl.F_GETFL) | os.O_NONBLOCK)
        return proc

    def _reInitCallback(self):
        del self.reInitHandler
        self.initStart()
        return False

    def _postInit(self):
        self.invoker.add(lambda: self.param.advertiser.advertiseMirrorSite(self.mirrorSite.id))
        if self.mirrorSite.updaterExe is not None:
            if self.mirrorSite.schedType == "interval":
                self.scheduler.addIntervalJob(self.mirrorSite.id, self.mirrorSite.schedExpr, self.updateStart)
            elif self.mirrorSite.schedType == "cron":
                self.scheduler.addCronJob(self.mirrorSite.id, self.mirrorSite.schedExpr, self.updateStart)
            else:
                assert False

    def _reMaintainCallback(self):
        del self.reMaintainHandler
        self.maintainStart()
        return False

    def __isInitialized(self):
        return os.path.exists(self.initFlagFile)

    def __setInitialized(self):
        McUtil.touchFile(self.initFlagFile)

    def __readLastUpdateDatetime(self):
        if not os.path.exists(self.lastUpdateDatetimeFile):
            return datetime.min
        with open(self.lastUpdateDatetimeFile, "r") as f:
            return datetime.strptime(f.read(), "%Y-%m-%d %H:%M")

    def __writeLastUpdateDatetime(self, schedDatetime):
        with open(self.lastUpdateDatetimeFile, "w") as f:
            f.write(schedDatetime.strftime("%Y-%m-%d %H:%M"))


class _ApiServer(UnixDomainSocketApiServer):

    def __init__(self, parent):
        self.updaterDict = parent.updaterDict
        super().__init__(McConst.apiServerFile,
                         self._clientAppearFunc,
                         None,                       # we track client life-time by its process object, not by clientDisappearFunc
                         self._clientNoitfyFunc)

    def _clientAppearFunc(self, sock):
        pid = McUtil.getUnixDomainSocketPeerInfo(sock)[0]
        for mirrorId, obj in self.updaterDict.items():
            if obj.proc is not None and obj.proc.pid == pid:
                return mirrorId
        raise Exception("client not found")

    def _clientNoitfyFunc(self, mirrorId, data):
        obj = self.updaterDict[mirrorId]

        if "message" not in data:
            raise Exception("\"message\" field does not exist in notification")
        if "data" not in data:
            raise Exception("\"data\" field does not exist in notification")

        if data["message"] == "progress":
            if "progress" not in data["data"]:
                raise Exception("\"data.progress\" field does not exist in notification")
            if not isinstance(data["data"]["progress"], int):
                raise Exception("\"data.progress\" field does not contain an integer value")
            if not (0 <= data["data"]["progress"] <= 100):
                raise Exception("\"data.progress\" must be in range [0,100]")

            if obj.mirrorSite.initializerExe is not None and obj.status == McMirrorSiteUpdater.MIRROR_SITE_UPDATE_STATUS_INITING:
                obj.initProgressCallback(data["data"]["progress"])
            elif obj.mirrorSite.updaterExe is not None and obj.status == McMirrorSiteUpdater.MIRROR_SITE_UPDATE_STATUS_UPDATING:
                obj.updateProgressCallback(data["data"]["progress"])
            else:
                assert False

            return

        if data["message"] == "error":
            if "exc_info" not in data["data"]:
                raise Exception("\"data.exc_info\" field does not exist in notification")

            if obj.mirrorSite.initializerExe is not None and obj.status == McMirrorSiteUpdater.MIRROR_SITE_UPDATE_STATUS_INITING:
                obj.initErrorCallback(data["data"]["exc_info"])
            elif obj.mirrorSite.updaterExe is not None and obj.status == McMirrorSiteUpdater.MIRROR_SITE_UPDATE_STATUS_UPDATING:
                obj.updateErrorCallback(data["data"]["exc_info"])
            elif obj.mirrorSite.maintainerExe is not None and obj.status == McMirrorSiteUpdater.MIRROR_SITE_UPDATE_STATUS_IDLE:
                obj.maintainErrorCallback(data["data"]["exc_info"])
            else:
                assert False
            return

        if data["message"] == "error-and-hold-for":
            if "seconds" not in data["data"]:
                raise Exception("\"data.seconds\" field does not exist in notification")
            if not isinstance(data["data"]["seconds"], int):
                raise Exception("\"data.seconds\" field does not contain an integer value")
            if data["data"]["seconds"] <= 0:
                raise Exception("\"data.seconds\" must be greater than 0")
            if "exc_info" not in data["data"]:
                raise Exception("\"data.exc_info\" field does not exist in notification")

            if obj.mirrorSite.initializerExe is not None and obj.status == McMirrorSiteUpdater.MIRROR_SITE_UPDATE_STATUS_INITING:
                obj.initErrorAndHoldForCallback(data["data"]["seconds"], data["data"]["exc_info"])
            elif obj.mirrorSite.updaterExe is not None and obj.status == McMirrorSiteUpdater.MIRROR_SITE_UPDATE_STATUS_UPDATING:
                obj.updateErrorAndHoldForCallback(data["data"]["seconds"], data["data"]["exc_info"])
            else:
                assert False
            return

        raise Exception("message type \"%s\" is not supported" % (data["message"]))


class _Scheduler:

    def __init__(self):
        self.jobDict = OrderedDict()       # dict<id,(type,param,callback)>
        self.jobPauseDict = dict()         # dict<id,datetime>

        self.nextDatetime = None
        self.nextJobList = None

        self.timeoutHandler = None

    def dispose(self):
        if self.timeoutHandler is not None:
            GLib.source_remove(self.timeoutHandler)
            self.timeoutHandler = None
        self.nextJobList = None
        self.nextDatetime = None
        self.jobDict = OrderedDict()

    def addCronJob(self, jobId, cronExpr, jobCallback):
        assert jobId not in self.jobDict

        now = datetime.now()

        # add job
        iter = self._cronCreateIter(cronExpr, now)
        self.jobDict[jobId] = ("cron", iter, jobCallback)

        # add job or recalcluate timeout if it is first job
        if self.nextDatetime is not None:
            now = min(now, self.nextDatetime)
            if self._cronGetNextDatetime(now, iter) < self.nextDatetime:
                self._clearTimeout()
                self._calcTimeout(now)
            elif self._cronGetNextDatetime(now, iter) == self.nextDatetime:
                self.nextJobList.append(jobId)
        else:
            self._calcTimeout(now)

    def addIntervalJob(self, jobId, intervalStr, jobCallback):
        assert jobId not in self.jobDict

        # get timedelta
        m = re.match("([0-9]+)(h|d|w|m)", intervalStr)
        if m is None:
            raise Exception("invalid interval %s" % (intervalStr))
        if m.group(2) == "h":
            interval = datetime.timedelta(hours=int(m.group(1)))
        elif m.group(2) == "d":
            interval = datetime.timedelta(days=int(m.group(1)))
        elif m.group(2) == "w":
            interval = datetime.timedelta(weeks=int(m.group(1)))
        elif m.group(2) == "m":
            interval = datetime.timedelta(months=int(m.group(1)))
        else:
            assert False

        # add job
        self.jobDict[jobId] = ("interval", interval, jobCallback)

        # add job or recalcluate timeout if it is first job
        now = datetime.now()
        if self.nextDatetime is not None:
            now = min(now, self.nextDatetime)
            if self._intervalGetNextDatetime(now, interval) < self.nextDatetime:
                self._clearTimeout()
                self._calcTimeout(now)
            elif self._intervalGetNextDatetime(now, interval) == self.nextDatetime:
                self.nextJobList.append(jobId)
        else:
            self._calcTimeout(now)

    def removeJob(self, jobId):
        assert jobId in self.jobDict

        # remove job
        del self.jobDict[jobId]

        # recalculate timeout if neccessary
        now = datetime.now()
        if self.nextDatetime is not None:
            if jobId in self.nextJobList:
                self.nextJobList.remove(jobId)
                if len(self.nextJobList) == 0:
                    self._clearTimeout()
                    self._calcTimeout(now)
        else:
            assert False

    def pauseJob(self, jobId, datetime):
        assert jobId in self.jobDict
        self.jobPauseDict[jobId] = datetime

    def _calcTimeout(self, now):
        assert self.nextDatetime is None

        for jobId, v in self.jobDict.items():
            if v[0] == "cron":
                iter = v[1]
                if self.nextDatetime is None or self._cronGetNextDatetime(now, iter) < self.nextDatetime:
                    self.nextDatetime = self._cronGetNextDatetime(now, iter)
                    self.nextJobList = [jobId]
                    continue
                if self._cronGetNextDatetime(now, iter) == self.nextDatetime:
                    self.nextJobList.append(jobId)
                    continue
            elif v[0] == "interval":
                interval = v[1]
                if self.nextDatetime is None or self._intervalGetNextDatetime(now, interval) < self.nextDatetime:
                    self.nextDatetime = self._intervalGetNextDatetime(now, interval)
                    self.nextJobList = [jobId]
                if self._intervalGetNextDatetime(now, interval) == self.nextDatetime:
                    self.nextJobList.append(jobId)
                    continue
            else:
                assert False

        if self.nextDatetime is not None:
            interval = math.ceil((self.nextDatetime - now).total_seconds())
            assert interval > 0
            self.timeoutHandler = GLib.timeout_add_seconds(interval, self._jobCallback)

    def _clearTimeout(self):
        assert self.nextDatetime is not None

        GLib.source_remove(self.timeoutHandler)
        self.timeoutHandler = None
        self.nextJobList = None
        self.nextDatetime = None

    def _jobCallback(self):
        for jobId in self.nextJobList:
            if jobId not in self.jobPauseDict:
                self.jobDict[jobId][2](self.nextDatetime)
            else:
                if self.jobPauseDict[jobId] <= self.nextDatetime:
                    del self.jobPauseDict[jobId]
                    self.jobDict[jobId][2](self.nextDatetime)
        self._clearTimeout()
        self._calcTimeout(datetime.now())           # self._calcTimeout(self.nextDatetime) is stricter but less robust
        return False

    def _cronCreateIter(self, cronExpr, curDatetime):
        iter = croniter(cronExpr, curDatetime, datetime)
        iter.get_next()
        return iter

    def _cronGetNextDatetime(self, curDatetime, croniterIter):
        while croniterIter.get_current() < curDatetime:
            croniterIter.get_next()
        return croniterIter.get_current()

    def _intervalGetNextDatetime(self, curDatetime, interval):
        return curDatetime + interval
