#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import argparse
import glob
import logging
import string
import unicodedata, re
import sys
import pickle

from geeknote import GeekNote
from storage import Storage
from editor import Editor
import tools

# set default logger (write log to file)
def_logpath = os.path.join(os.getenv('USERPROFILE') or os.getenv('HOME'),  'GeekNoteSync.log')
formatter = logging.Formatter('%(asctime)-15s : %(message)s')
handler = logging.FileHandler(def_logpath)
handler.setFormatter(formatter)

mtime_file_extention = '.mtime'
rej_file_extension = '.rej'

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
logger.addHandler(handler)

# determine if this is a narrow build or wide build (or py3k)
try:
    unichr(0x10000)
    MAX_CHAR = 0x110000
except ValueError:
    MAX_CHAR = 0x9999

# http://stackoverflow.com/a/93029
CONTROL_CHARS = ''.join(c for c in (unichr(i) for i in xrange(MAX_CHAR)) \
                if c not in string.printable and unicodedata.category(c) == 'Cc')
CONTROL_CHARS_RE = re.compile('[%s]' % re.escape(CONTROL_CHARS))
def remove_control_characters(s):
    return CONTROL_CHARS_RE.sub('', s)

def log(func):
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception, e:
            logger.error("%s", str(e))
    return wrapper


@log
def reset_logpath(logpath):
    """
    Reset logpath to path from command line
    """
    global logger

    if not logpath:
        return

    # remove temporary log file if it's empty
    if os.path.isfile(def_logpath):
        if os.path.getsize(def_logpath) == 0:
            os.remove(def_logpath)

    # save previous handlers
    handlers = logger.handlers

    # remove old handlers
    for handler in handlers:
        logger.removeHandler(handler)

    # try to set new file handler
    handler = logging.FileHandler(logpath)
    handler.setFormatter(formatter)
    logger.addHandler(handler)

def all_notebooks():
    geeknote = GeekNote()
    return [notebook.name for notebook in geeknote.findNotebooks()]

class GNSync:

    notebook_name = None
    path = None
    mask = None
    sync_up = False
    sync_down = False
    merged = None

    notebook_guid = None
    all_set = False
    mtime_file = None

    @log
    def __init__(self, notebook_name, path, mask, format, sync_mode, merged=False):
        # check auth
        if not Storage().getUserToken():
            raise Exception("Auth error. There is not any oAuthToken.")

        #set path
        if not path:
            raise Exception("Path to sync directories does not select.")

        if not os.path.exists(path):
            raise Exception("Path to sync directories does not exist.")

        self.path = path
        self.mtime_file = os.path.join(self.path, '.' + notebook_name + mtime_file_extention)
        if not os.path.exists(self.mtime_file):
                open(self.mtime_file, 'wb') # create the mtime file

        #set mask
        if not mask:
            mask = "*.*"

        self.mask = mask

        #set format
        if not format:
            format = "plain"

        self.format = format

        if format == "markdown":
            self.extension = ".md"
        else:
            self.extension = ".txt"

        if sync_mode == "up":
            self.sync_up = True
        elif sync_mode == "down":
            self.sync_down = True
        else:
            self.sync_up = True
            self.sync_down = True

        self.merged = merged

        logger.info('Sync Start')

        #set notebook
        self.notebook_guid,\
        self.notebook_name = self._get_notebook(notebook_name, path)

        # all is Ok
        self.all_set = True

    @log
    def sync(self):
        """
        Synchronize files to notes
        """
        if not self.all_set:
            return

        files = self._get_files()
        notes = self._get_notes()

        mtimes = self._get_mtimes()
        if mtimes is None:
            mtimes = {}

        #track files and notes changed in this sync.
        updated = []

        if self.sync_up:
            logger.info('Uploading notes')
            for f in files:
                has_note = False
                for n in notes:
                    if f['name'] == n.title:
                        has_note = True
                        #get saved mtime for the note
                        mtime = mtimes.get(n.title, 0)
                        if mtime != 0:
                            if mtime['file_mtime'] == f['mtime'] and        \
                                mtime['note_mtime'] == n.updated:
                                #both have not changed since last sync
                                break
                            elif mtime['file_mtime'] != f['mtime'] and        \
                                mtime['note_mtime'] != n.updated:
                                #both changed, need merge
                                if self.merged:
                                    self._update_note(f, n)
                                    updated.append(f['name'])
                                else:
                                    logger.warning('Skipped note (CONFLICT!!!): {0},\n'
                                    'merge the note manually and sync the '
                                    'notebook again with --merged option'.format(f['name']))
                                    self._create_rej_file(n)
                                break
                            elif mtime['file_mtime'] != f['mtime']:
                                #only local note changed
                                self._update_note(f, n)
                                updated.append(f['name'])
                            else:
                                #only server note changed, handle it in twoway mode
                                pass
                        else:
                            if f['mtime'] > n.updated:
                                self._update_note(f, n)
                                updated.append(f['name'])

                        break

                if not has_note:
                    self._create_note(f)
                    updated.append(f['name'])

        if self.sync_down:
            logger.info('Downloading notes')
            for n in notes:
                has_file = False
                for f in files:
                    if f['name'] == n.title:
                            has_file = True
                            mtime = mtimes.get(n.title, 0)
                            if mtime != 0:
                                if mtime['file_mtime'] == f['mtime'] and \
                                    mtime['note_mtime'] == n.updated:
                                    break
                                elif mtime['file_mtime'] != f['mtime'] and \
                                    mtime['note_mtime'] != n.updated:
                                    break #handled already
                                elif mtime['note_mtime'] != n.updated:
                                    #only server note changed
                                    self._update_file(f, n)
                                    updated.append(n.title)
                                else:
                                    pass #handled already
                            else:
                                if f['mtime'] < n.updated:
                                    self._update_file(f, n)
                                    updated.append(n.title)

                            break

                if not has_file:
                    self._create_file(n)
                    updated.append(n.title)

        # after sync, save the mtimes of both files and notes
        files = self._get_files()
        notes = self._get_notes()
        for f in files:
            for n in notes:
                if f['name'] == n.title and n.title in updated:
                    mtimes[n.title] = {'file_mtime':f['mtime'], 'note_mtime':n.updated}

        self._save_mtimes(mtimes)

        logger.info('Sync Complete')

    @log
    def _update_note(self, file_note, note):
        """
        Updates note from file
        """
        content = self._get_file_content(file_note['path'])

        result = GeekNote().updateNote(
            guid=note.guid,
            title=note.title,
            content=content,
            notebook=self.notebook_guid)

        if result:
            logger.info('Note "{0}" was updated'.format(note.title))
        else:
            raise Exception('Note "{0}" was not updated'.format(note.title))

        return result

    @log
    def _update_file(self, file_note, note):
        """
        Updates file from note
        """
        GeekNote().loadNoteContent(note)
        content = Editor.ENMLtoText(note.content)
        open(file_note['path'], "w").write(content)
        logger.info('File "{0}" was updated'.format(os.path.basename(file_note['path'])))

    @log
    def _create_note(self, file_note):
        """
        Creates note from file
        """

        content = self._get_file_content(file_note['path'])

        if content is None:
            return

        result = GeekNote().createNote(
            title=file_note['name'],
            content=content,
            notebook=self.notebook_guid,
            created=file_note['mtime'])

        if result:
            logger.info('Note "{0}" was created'.format(file_note['name']))
        else:
            raise Exception('Note "{0}" was not' \
                            ' created'.format(file_note['name']))

        return result

    @log
    def _create_file(self, note):
        """
        Creates file from note
        """
        GeekNote().loadNoteContent(note)
        content = Editor.ENMLtoText(note.content)
        path = os.path.join(self.path, note.title + self.extension)
        open(path, "w").write(content)
        logger.info('File "{0}" was created'.format(os.path.basename(path)))
        return True

    @log
    def _create_rej_file(self, note):
        """
        Create reject file for note
        """
        GeekNote().loadNoteContent(note)
        content = Editor.ENMLtoText(note.content)
        rej_file = "." + note.title + self.extension + rej_file_extension
        path = os.path.join(self.path, rej_file)
        open(path, "w").write(content)
        logger.info('Created reject file {0} for note: {1}'.format(rej_file, note.title))
        return True

    @log
    def _get_file_content(self, path):
        """
        Get file content.
        """
        content = open(path, "r").read()

        # strip unprintable characters
        content = remove_control_characters(content.decode('utf-8')).encode('utf-8')
        content = Editor.textToENML(content=content, raise_ex=True, format=self.format)

        if content is None:
            logger.warning("File {0}. Content must be " \
                           "an UTF-8 encode.".format(path))
            return None

        return content

    @log
    def _get_notebook(self, notebook_name, path):
        """
        Get notebook guid and name.
        Takes default notebook if notebook's name does not select.
        """
        notebooks = GeekNote().findNotebooks()

        if not notebook_name:
            notebook_name = os.path.basename(os.path.realpath(path))

        notebook = [item for item in notebooks if item.name == notebook_name]
        guid = None
        if notebook:
            guid = notebook[0].guid

        if not guid:
            notebook = GeekNote().createNotebook(notebook_name)

            if(notebook):
                logger.info('Notebook "{0}" was' \
                            ' created'.format(notebook_name))
            else:
                raise Exception('Notebook "{0}" was' \
                                ' not created'.format(notebook_name))

            guid = notebook.guid

        return (guid, notebook_name)

    @log
    def _get_files(self):
        """
        Get files by self.mask from self.path dir.
        """

        file_paths = glob.glob(os.path.join(self.path, self.mask))

        files = []
        for f in file_paths:
            if os.path.isfile(f):
                file_name = os.path.basename(f)
                file_name = os.path.splitext(file_name)[0]

                mtime = int(os.path.getmtime(f) * 1000)

                files.append({'path': f, 'name': file_name, 'mtime': mtime})

        return files

    @log
    def _get_notes(self):
        """
        Get notes from evernote.
        """
        keywords = 'notebook:"{0}"'.format(tools.strip(self.notebook_name))
        return GeekNote().findNotes(keywords, 10000).notes

    @log
    def _get_mtimes(self):
        """
        Get modification times
        """
        mtfile = open(self.mtime_file, 'rb')
        mtimes = pickle.load(mtfile)
        return mtimes

    @log
    def _save_mtimes(self, mtimes):
        """
        Save modification times
        """
        mtfile = open(self.mtime_file, 'wb')
        pickle.dump(mtimes, mtfile)

def main():
    try:
        parser = argparse.ArgumentParser()
        parser.add_argument('--path', '-p', action='store', help='Path to synchronize directory')
        parser.add_argument('--mask', '-m', action='store', help='Mask of files to synchronize. Default is "*.*"')
        parser.add_argument('--format', '-f', action='store', default='plain', choices=['plain', 'markdown'], help='The format of the file contents. Default is "plain". Valid values are "plain" and "markdown"')
        parser.add_argument('--notebook', '-n', action='store', help='Notebook name for synchronize. Default is default notebook unless all is selected')
        parser.add_argument('--logpath', '-l', action='store', help='Path to log file. Default is GeekNoteSync in home dir')
        parser.add_argument('--two-way', '-t', action='store_true', help='Two-way sync. Upload changed notes to server first and then download.')
        parser.add_argument('--all', '-a', action='store_true', help='Synchronize all notebooks', default=False)
        parser.add_argument('--verbose', '-v', action='store_true', help='Verbose mode. Let GNSync print more logs about its progress')
        parser.add_argument('--one-way', '-w', action='store', default='up', choices=['up', 'down'], help='''One-way sync. Upload changed notes to server or download changes from server if "up" or "down" is given respectively. If --two-way option is specified, --one-way option will be ignored. If both are not given, GNSync will default to do one-way upload.''')
        parser.add_argument('--merged', '-M', action='store_true', help='Update merged notes to server. Specify it only when conflicts are reported and you have resolved all of them manually on your local files')

        args = parser.parse_args()

        path = args.path if args.path else None
        mask = args.mask if args.mask else None
        format = args.format if args.format else None
        notebook = args.notebook if args.notebook else None
        logpath = args.logpath if args.logpath else None
        twoway = True if args.two_way else False
        oneway = args.one_way if args.one_way else "up"
        merged = True if args.merged else False

        sync_mode = "up" #default sync mode
        if twoway:
            sync_mode = "twoway"
        else:
            sync_mode = oneway

        reset_logpath(logpath)

        if args.verbose:
            handler = logging.StreamHandler(sys.stdout)
            formatter = logging.Formatter('%(message)s')
            handler.setFormatter(formatter)
            logger.addHandler(handler)

        if args.all:
            for notebook in all_notebooks():
                notebook_path = os.path.join(path, notebook)
                if not os.path.exists(notebook_path):
                    os.mkdir(notebook_path)
                GNS = GNSync(notebook, notebook_path, mask, format, sync_mode, merged)
                GNS.sync()
        else:
            GNS = GNSync(notebook, path, mask, format, sync_mode, merged)
            GNS.sync()

    except (KeyboardInterrupt, SystemExit, tools.ExitException):
        pass

    except Exception, e:
        logger.error(str(e))

if __name__ == "__main__":
    main()
