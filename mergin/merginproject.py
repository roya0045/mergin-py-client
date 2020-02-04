
import json
import math
import os
import re
import shutil
import uuid
from datetime import datetime
from dateutil.tz import tzlocal

from .common import UPLOAD_CHUNK_SIZE
from .utils import generate_checksum, move_file, int_version, find


this_dir = os.path.dirname(os.path.realpath(__file__))


try:
    from .deps import pygeodiff
except ImportError:
    os.environ['GEODIFF_ENABLED'] = 'False'


class InvalidProject(Exception):
    pass


class MerginProject:
    """ Base class for Mergin local projects.

    Linked to existing local directory, with project metadata (mergin.json) and backups located in .mergin directory.
    """
    def __init__(self, directory):
        self.dir = os.path.abspath(directory)
        if not os.path.exists(self.dir):
            raise InvalidProject('Project directory does not exist')

        # make sure we can load correct pygeodiff
        if os.environ.get('GEODIFF_ENABLED', 'True').lower() == 'true':
            try:
                self.geodiff = pygeodiff.GeoDiff()
            except pygeodiff.geodifflib.GeoDiffLibVersionError:
                self.geodiff = None
        else:
            self.geodiff = None

        self.meta_dir = os.path.join(self.dir, '.mergin')
        if not os.path.exists(self.meta_dir):
            os.mkdir(self.meta_dir)

    def fpath(self, file, other_dir=None):
        """
        Helper function to get absolute path of project file. Defaults to project dir but
        alternative dir get be provided (mostly meta or temp). Also making sure that parent dirs to file exist.

        :param file: relative file path in project (posix)
        :type file: str
        :param other_dir: alternative base directory for file, defaults to None
        :type other_dir: str
        :returns: file's absolute path
        :rtype: str
        """
        root = other_dir or self.dir
        abs_path = os.path.abspath(os.path.join(root, file))
        f_dir = os.path.dirname(abs_path)
        os.makedirs(f_dir, exist_ok=True)
        return abs_path

    def fpath_meta(self, file):
        """ Helper function to get absolute path of file in meta dir. """
        return self.fpath(file, self.meta_dir)

    @property
    def metadata(self):
        if not os.path.exists(self.fpath_meta('mergin.json')):
            raise InvalidProject('Project metadata has not been created yet')
        with open(self.fpath_meta('mergin.json'), 'r') as file:
            return json.load(file)

    @metadata.setter
    def metadata(self, data):
        with open(self.fpath_meta('mergin.json'), 'w') as file:
            json.dump(data, file, indent=2)

    def is_versioned_file(self, file):
        """ Check if file is compatible with geodiff lib and hence suitable for versioning.

        :param file: file path
        :type file: str
        :returns: if file is compatible with geodiff lib
        :rtype: bool
        """
        if not self.geodiff:
            return False
        diff_extensions = ['.gpkg', '.sqlite']
        f_extension = os.path.splitext(file)[1]
        return f_extension in diff_extensions

    def ignore_file(self, file):
        """
        Helper function for blacklisting certain types of files.

        :param file: file path in project
        :type file: str
        :returns: whether file should be ignored
        :rtype: bool
        """
        ignore_ext = re.compile(r'({})$'.format('|'.join(re.escape(x) for x in ['-shm', '-wal', '~', 'pyc', 'swap'])))
        ignore_files = ['.DS_Store', '.directory']
        name, ext = os.path.splitext(file)
        if ext and ignore_ext.search(ext):
            return True
        if file in ignore_files:
            return True
        return False

    def inspect_files(self):
        """
        Inspect files in project directory and return metadata.

        :returns: metadata for files in project directory in server required format
        :rtype: list[dict]
        """
        files_meta = []
        for root, dirs, files in os.walk(self.dir, topdown=True):
            dirs[:] = [d for d in dirs if d not in ['.mergin']]
            for file in files:
                if self.ignore_file(file):
                    continue
                abs_path = os.path.abspath(os.path.join(root, file))
                rel_path = os.path.relpath(abs_path, start=self.dir)
                proj_path = '/'.join(rel_path.split(os.path.sep))  # we need posix path
                files_meta.append({
                    "path": proj_path,
                    "checksum": generate_checksum(abs_path),
                    "size": os.path.getsize(abs_path),
                    "mtime": datetime.fromtimestamp(os.path.getmtime(abs_path), tzlocal())
                })
        return files_meta

    def compare_file_sets(self, origin, current):
        """
        Helper function to calculate difference between two sets of files metadata using file names and checksums.

        :Example:

        >>> origin = [{'checksum': '08b0e8caddafe74bf5c11a45f65cedf974210fed', 'path': 'base.gpkg', 'size': 2793, 'mtime': '2019-08-26T11:08:34.051221+02:00'}]
        >>> current = [{'checksum': 'c9a4fd2afd513a97aba19d450396a4c9df8b2ba4', 'path': 'test.qgs', 'size': 31980, 'mtime': '2019-08-26T11:09:30.051221+02:00'}]
        >>> self.compare_file_sets(origin, current)
        {"added": [{'checksum': 'c9a4fd2afd513a97aba19d450396a4c9df8b2ba4', 'path': 'test.qgs', 'size': 31980, 'mtime': '2019-08-26T11:09:30.051221+02:00'}], "removed": [[{'checksum': '08b0e8caddafe74bf5c11a45f65cedf974210fed', 'path': 'base.gpkg', 'size': 2793, 'mtime': '2019-08-26T11:08:34.051221+02:00'}]], "renamed": [], "updated": []}

        :param origin: origin set of files metadata
        :type origin: list[dict]
        :param current: current set of files metadata to be compared against origin
        :type current: list[dict]
        :returns: changes between two sets with change type
        :rtype: dict[str, list[dict]]'
        """
        origin_map = {f["path"]: f for f in origin}
        current_map = {f["path"]: f for f in current}
        removed = [f for f in origin if f["path"] not in current_map]

        added = []
        for f in current:
            if f["path"] in origin_map:
                continue
            added.append(f)

        moved = []
        for rf in removed:
            match = find(
                current,
                lambda f: f["checksum"] == rf["checksum"] and f["size"] == rf["size"] and all(
                    f["path"] != mf["path"] for mf in moved)
            )
            if match:
                moved.append({**rf, "new_path": match["path"]})

        added = [f for f in added if all(f["path"] != mf["new_path"] for mf in moved)]
        removed = [f for f in removed if all(f["path"] != mf["path"] for mf in moved)]

        updated = []
        for f in current:
            path = f["path"]
            if path not in origin_map:
                continue
            if f["checksum"] == origin_map[path]["checksum"]:
                continue
            f["origin_checksum"] = origin_map[path]["checksum"]
            updated.append(f)

        return {
            "renamed": moved,
            "added": added,
            "removed": removed,
            "updated": updated
        }

    def get_pull_changes(self, server_files):
        """
        Calculate changes needed to be pulled from server.

        Calculate diffs between local files metadata and server's ones. Because simple metadata like file size or
        checksum are not enough to determine geodiff files changes, evaluate also their history (provided by server).
        For small files ask for full versions of geodiff files, otherwise determine list of diffs needed to update file.

        .. seealso:: self.compare_file_sets

        :param server_files: list of server files' metadata (see also self.inspect_files())
        :type server_files: list[dict]
        :returns: changes metadata for files to be pulled from server
        :rtype: dict
        """
        changes = self.compare_file_sets(self.metadata['files'], server_files)
        if not self.geodiff:
            return changes

        size_limit = int(os.environ.get('DIFFS_LIMIT_SIZE', 1024 * 1024)) # with smaller values than limit download full file instead of diffs
        not_updated = []
        for file in changes['updated']:
            # for small geodiff files it does not make sense to download diff and then apply it (slow)
            if not self.is_versioned_file(file["path"]):
                continue

            diffs = []
            diffs_size = 0
            is_updated = False
            # need to track geodiff file history to see if there were any changes
            for k, v in file['history'].items():
                if int_version(k) <= int_version(self.metadata['version']):
                    continue  # ignore history of no interest
                is_updated = True
                if 'diff' in v:
                    diffs.append(v['diff']['path'])
                    diffs_size += v['diff']['size']
                else:
                    diffs = []
                    break  # we found force update in history, does not make sense to download diffs

            if is_updated:
                if diffs and file['size'] > size_limit and diffs_size < file['size']/2:
                    file['diffs'] = diffs
            else:
                not_updated.append(file)

        changes['updated'] = [f for f in changes['updated'] if f not in not_updated]
        return changes

    def get_push_changes(self):
        """
        Calculate changes needed to be pushed to server.

        Calculate diffs between local files metadata and actual files in project directory. Because simple metadata like
        file size or checksum are not enough to determine geodiff files changes, geodiff tool is used to determine change
        of file content and update corresponding metadata.

        .. seealso:: self.compare_file_sets

        :returns: changes metadata for files to be pushed to server
        :rtype: dict
        """
        changes = self.compare_file_sets(self.metadata['files'], self.inspect_files())
        for file in changes['added'] + changes['updated']:
            file['chunks'] = [str(uuid.uuid4()) for i in range(math.ceil(file["size"] / UPLOAD_CHUNK_SIZE))]

        if not self.geodiff:
            return changes

        # need to check for for real changes in geodiff files using geodiff tool (comparing checksum is not enough)
        not_updated = []
        for file in changes['updated']:
            path = file["path"]
            if not self.is_versioned_file(path):
                continue

            current_file = self.fpath(path)
            origin_file = self.fpath(path, self.meta_dir)
            diff_id = str(uuid.uuid4())
            diff_name = path + '-diff-' + diff_id
            diff_file = self.fpath_meta(diff_name)
            try:
                self.geodiff.create_changeset(origin_file, current_file, diff_file)
                if self.geodiff.has_changes(diff_file):
                    diff_size = os.path.getsize(diff_file)
                    file['checksum'] = file['origin_checksum']  # need to match basefile on server
                    file['chunks'] = [str(uuid.uuid4()) for i in range(math.ceil(diff_size / UPLOAD_CHUNK_SIZE))]
                    file['mtime'] = datetime.fromtimestamp(os.path.getmtime(current_file), tzlocal())
                    file['diff'] = {
                        "path": diff_name,
                        "checksum": generate_checksum(diff_file),
                        "size": diff_size,
                        'mtime': datetime.fromtimestamp(os.path.getmtime(diff_file), tzlocal())
                    }
                else:
                    not_updated.append(file)
            except (pygeodiff.GeoDiffLibError, pygeodiff.GeoDiffLibConflictError) as e:
                pass  # we do force update

        changes['updated'] = [f for f in changes['updated'] if f not in not_updated]
        return changes

    def get_list_of_push_changes(self, push_changes):
        changes = {}
        for idx, file in enumerate(push_changes["updated"]):
            if "diff" in file:
                changeset_path = file["diff"]["path"]
                changeset = self.fpath_meta(changeset_path)
                result_file = self.fpath("change_list" + str(idx), self.meta_dir)
                try:
                    self.geodiff.list_changes_summary(changeset, result_file)
                    with open(result_file, 'r') as f:
                        change = f.read()
                        changes[file["path"]] = json.loads(change)
                    os.remove(result_file)
                except (pygeodiff.GeoDiffLibError, pygeodiff.GeoDiffLibConflictError):
                    pass
        return changes

    def apply_pull_changes(self, changes, temp_dir):
        """
        Apply changes pulled from server.

        Update project files according to file changes. Apply changes to geodiff basefiles as well
        so they are up to date with server. In case of conflicts create backups from locally modified versions.

        .. seealso:: self.pull_changes

        :param changes: metadata for pulled files
        :type changes: dict[str, list[dict]]
        :param temp_dir: directory with downloaded files from server
        :type temp_dir: str
        :returns: files where conflicts were found
        :rtype: list[str]
        """
        conflicts = []
        local_changes = self.get_push_changes()
        modified = {}
        for f in local_changes["added"] + local_changes["updated"]:
            modified.update({f['path']: f})
        for f in local_changes["renamed"]:
            modified.update({f['new_path']: f})

        local_files_map = {}
        for f in self.inspect_files():
            local_files_map.update({f['path']: f})

        for k, v in changes.items():
            for item in v:
                path = item['path'] if k != 'renamed' else item['new_path']
                src = self.fpath(path, temp_dir) if k != 'renamed' else self.fpath(item["path"])
                dest = self.fpath(path)
                basefile = self.fpath_meta(path)

                # special care is needed for geodiff files
                # 'src' here is server version of file and 'dest' is locally modified
                if self.is_versioned_file(path) and k == 'updated':
                    if path in modified:
                        server_diff = self.fpath(f'{path}-server_diff', temp_dir)  # diff between server file and local basefile
                        local_diff = self.fpath(f'{path}-local_diff', temp_dir)

                        # temporary backup of file pulled from server for recovery
                        f_server_backup = self.fpath(f'{path}-server_backup', temp_dir)
                        shutil.copy(src, f_server_backup)

                        # create temp backup (ideally with geodiff) of locally modified file if needed later
                        f_conflict_file = self.fpath(f'{path}-local_backup', temp_dir)
                        try:
                            self.geodiff.create_changeset(basefile, dest, local_diff)
                            shutil.copy(basefile, f_conflict_file)
                            self.geodiff.apply_changeset(f_conflict_file, local_diff)
                        except (pygeodiff.GeoDiffLibError, pygeodiff.GeoDiffLibConflictError):
                            # FIXME hard copy can lead to data loss if changes from -wal file were not flushed !!!
                            shutil.copy(dest, f_conflict_file)

                        # try to do rebase magic
                        try:
                            self.geodiff.create_changeset(basefile, src, server_diff)
                            self.geodiff.rebase(basefile, src, dest)
                            # make sure basefile is in the same state as remote server file (for calc of push changes)
                            self.geodiff.apply_changeset(basefile, server_diff)
                        except (pygeodiff.GeoDiffLibError, pygeodiff.GeoDiffLibConflictError) as err:
                            # it would not be possible to commit local changes, they need to end up in new conflict file
                            shutil.copy(f_conflict_file, dest)  # revert file
                            conflict = self.backup_file(path)
                            conflicts.append(conflict)
                            # original file synced with server
                            shutil.copy(f_server_backup, basefile)
                            shutil.copy(f_server_backup, dest)
                            # changes in -wal have been already applied in conflict file or LOST (see above)
                            if os.path.exists(f'{dest}-wal'):
                                os.remove(f'{dest}-wal')
                            if os.path.exists(f'{dest}-shm'):
                                os.remove(f'{dest}-shm')
                    else:
                        # just use server version of file to update both project file and its basefile
                        shutil.copy(src, dest)
                        shutil.copy(src, basefile)
                else:
                    # backup if needed
                    if path in modified and item['checksum'] != local_files_map[path]['checksum']:
                        conflict = self.backup_file(path)
                        conflicts.append(conflict)

                    if k == 'removed':
                        os.remove(dest)
                        if self.is_versioned_file(path):
                            os.remove(basefile)
                    elif k == 'renamed':
                        move_file(src, dest)
                        if self.is_versioned_file(path):
                            move_file(self.fpath_meta(item["path"]), basefile)
                    else:
                        shutil.copy(src, dest)
                        if self.is_versioned_file(path):
                            shutil.copy(src, basefile)

        return conflicts

    def apply_push_changes(self, changes):
        """
        For geodiff files update basefiles according to changes pushed to server.

        :param changes: metadata for pulled files
        :type changes: dict[str, list[dict]]
        """
        if not self.geodiff:
            return
        for k, v in changes.items():
            for item in v:
                path = item['path'] if k != 'renamed' else item['new_path']
                if not self.is_versioned_file(path):
                    continue

                basefile = self.fpath_meta(path)
                if k == 'renamed':
                    move_file(self.fpath_meta(item["path"]), basefile)
                elif k == 'removed':
                    os.remove(basefile)
                elif k == 'added':
                    shutil.copy(self.fpath(path), basefile)
                elif k == 'updated':
                    # in case for geopackage cannot be created diff
                    if "diff" not in item:
                        continue
                    # better to apply diff to previous basefile to avoid issues with geodiff tmp files
                    changeset = self.fpath_meta(item['diff']['path'])
                    patch_error = self.apply_diffs(basefile, [changeset])
                    if patch_error:
                        # in case of local sync issues it is safier to remove basefile, next time it will be downloaded from server
                        os.remove(basefile)
                else:
                    pass

    def backup_file(self, file):
        """
        Create backup file next to its origin.

        :param file: path of file in project
        :type file: str
        :returns: path to backupfile
        :rtype: str
        """
        src = self.fpath(file)
        if not os.path.exists(src):
            return
        backup_path = self.fpath(f'{file}_conflict_copy')
        index = 2
        while os.path.exists(backup_path):
            backup_path = self.fpath(f'{file}_conflict_copy{index}')
            index += 1
        shutil.copy(src, backup_path)
        return backup_path

    def apply_diffs(self, basefile, diffs):
        """
        Helper function to update content of geodiff file using list of diffs.
        Input file will be overwritten (make sure to create backup if needed).

        :param basefile: abs path to file to be updated
        :type basefile: str
        :param diffs: list of abs paths to geodiff changeset files
        :type diffs: list[str]
        :returns: error message if diffs were not successfully applied or None
        :rtype: str
        """
        error = None
        if not self.is_versioned_file(basefile):
            return error

        for index, diff in enumerate(diffs):
            try:
                self.geodiff.apply_changeset(basefile, diff)
            except (pygeodiff.GeoDiffLibError, pygeodiff.GeoDiffLibConflictError) as e:
                error = str(e)
                break
        return error