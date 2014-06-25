""" Interface to btrfs-tools for snapshots. """

import subprocess
import os.path
import os

import logging
logger = logging.getLogger(__name__)
# logger.setLevel('DEBUG')

theChunkSize = 100 * (2**20)

DEVNULL = open(os.devnull, 'wb')


class Butter:

    """ Interface to local btrfs file system snapshots. """

    def __init__(self, path):
        """ Initialize.

        path indicates the btrfs volume, and also the directory containing snapshots.
        """
        btrfsVersionString = subprocess.check_output(
            ["btrfs", "--version"]
            ).decode("utf-8").strip()

        btrfsVersion = [int(num) for num in btrfsVersionString[7:].split(".")]

        if btrfsVersion < [3, 14]:
            logger.warn(
                "%s is not supported.  Please upgrade your btrfs to at least 3.14",
                btrfsVersionString
                )
        else:
            logger.debug("%s", btrfsVersionString)

        self.btrfsVersion = btrfsVersionString

        self.userPath = path

        # Get tree ID of the containing subvolume of path.
        self.mountID = int(subprocess.check_output(
            ["btrfs", "inspect", "rootid", self.userPath]
        ).decode("utf-8"))

        self.mountPath = subprocess.check_output(
            ["df", path]
        ).decode("utf-8").rsplit(None, 1)[1]

        self.relPath = os.path.relpath(path, self.mountPath)

        butterPath = subprocess.check_output(
            ["btrfs", "inspect", "subvol", str(self.mountID), self.mountPath]
        ).decode("utf-8").strip()
        self.butterPath = "<FS_TREE>/" + os.path.normpath(os.path.join(butterPath, self.relPath))

        logger.debug(
            "MountID: %d, Mount: %s, Path: %s",
            self.mountID, self.mountPath, self.butterPath
            )

        self.volumes = self._getVolumes()

    def listVolumes(self):
        """ List all read-only volumes under path. """
        return self.volumes

    def _getVolumes(self, readOnly=True):
        vols = {}
        volsByID = {}

        logger.info('Listing "%s" snapshots...', self.relPath)

        subprocess.check_call(["btrfs", "fi", "sync", self.mountPath], stdout=DEVNULL)

        result = subprocess.check_output(
            ["btrfs", "sub", "list", "-puta", "-r" if readOnly else "", self.mountPath]
        ).decode("utf-8")

        logger.debug("User path in btrfs: %s", self.butterPath)

        for line in result.splitlines()[2:]:
            logger.debug("%s", line)

            (id, gen, parent, top, uuid, path) = line.split()

            extra = not path.startswith(self.butterPath)

            logger.debug("%s snapshot path in btrfs: %s", "Extra" if extra else "REQUIRED", path)

            vol = {
                'id': int(id),
                'gen': int(gen),
                'parent': int(parent),
                # 'top': int(top),
                'uuid': uuid,
                'path': path if extra else os.path.relpath(path, self.butterPath),
                'extra': extra,
            }

            vols[uuid] = vol
            volsByID[int(id)] = vol

        try:
            usage = subprocess.check_output(["btrfs", "qgroup", "show", self.mountPath])
        except subprocess.CalledProcessError:
            logger.warn("Rescanning subvolume sizes (this may take a while)...")
            subprocess.check_call(["btrfs", "quota", "enable", self.mountPath])
            subprocess.check_call(["btrfs", "quota", "rescan", "-w", self.mountPath])
            usage = subprocess.check_output(["btrfs", "qgroup", "show", self.mountPath])

        for line in usage.splitlines()[2:]:
            (qgroup, totalSize, exclusiveSize) = line.split()
            volID = int(qgroup.split("/")[-1])

            if volID in volsByID:
                logger.debug("Snap info: %s", line)
                volsByID[volID]['totalSize'] = int(totalSize)
                volsByID[volID]['exclusiveSize'] = int(exclusiveSize)

        return vols

    def receive(self):
        """ Return a file-like (stream) object to store a diff. """
        cmd = ["btrfs", "receive", self.userPath]
        process = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        return process.stdin

    def send(self, uuid, parent, streamContext):
        """ Write a (incremental) snapshot to the stream context manager. """
        targetPath = os.path.normpath(os.path.join(self.userPath, self.volumes[uuid]['path']))

        if parent is not None:
            parentPath = os.path.normpath(os.path.join(
                self.userPath,
                self.volumes[parent]['path']
                ))
            cmd = ["btrfs", "send", "-p", parentPath, targetPath]
        else:
            cmd = ["btrfs", "send", targetPath]

        logger.debug("Command: %s", cmd)

        process = subprocess.Popen(cmd, stdout=subprocess.PIPE)

        try:
            streamContext.metadata['btrfsVersion'] = self.btrfsVersion
        except AttributeError:
            pass

        with streamContext as stream:
            while True:
                data = process.stdout.read(theChunkSize)
                if len(data) == 0:
                    break
                stream.write(data)
