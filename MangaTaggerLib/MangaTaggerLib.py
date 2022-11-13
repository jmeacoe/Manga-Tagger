import logging
import time
import re
import requests
import unicodedata
import shutil
import json

from datetime import datetime
from os import path
from pathlib import Path
from xml.etree.ElementTree import SubElement, Element, Comment, tostring
from xml.dom.minidom import parseString
from zipfile import ZipFile
from bs4 import BeautifulSoup

from MangaTaggerLib._version import __version__
from MangaTaggerLib.api import AniList, AniListRateLimit
from MangaTaggerLib.database import MetadataTable, ProcFilesTable, ProcSeriesTable
from MangaTaggerLib.errors import FileAlreadyProcessedError, FileUpdateNotRequiredError, UnparsableFilenameError, \
    MangaNotFoundError, MangaMatchedException
from MangaTaggerLib.models import Metadata
from MangaTaggerLib.task_queue import QueueWorker
from MangaTaggerLib.utils import AppSettings, compare

# Global Variable Declaration
LOG = logging.getLogger('MangaTaggerLib.MangaTaggerLib')

SKIP_MANGA = set()
CURRENTLY_PENDING_RENAME = set()
# CURRENTLY_PENDING_API_REQUEST = set()


def main():
    AppSettings.load()

    LOG.info(f'Starting Manga Tagger - Version {__version__}')
    LOG.debug('RUNNING WITH DEBUG LOG')

    if AppSettings.mode_settings is not None:
        LOG.info('DRY RUN MODE ENABLED')
        LOG.info(f"MetadataTable Insertion: {AppSettings.mode_settings['database_insert']}")
        LOG.info(f"Renaming Files: {AppSettings.mode_settings['rename_file']}")
        LOG.info(f"Writing Comicinfo.xml: {AppSettings.mode_settings['write_comicinfo']}")

    QueueWorker.run()


def process_manga_chapter(file_path: Path, event_id):
    filename = file_path.name
    directory_path = file_path.parent
    directory_name = file_path.parent.name

    logging_info = {
        'event_id': event_id,
        'manga_title': directory_name,
        "original_filename": filename
    }

    LOG.info(f'Now processing "{file_path}"...', extra=logging_info)

    LOG.debug(f'filename: {filename}')
    LOG.debug(f'directory_path: {directory_path}')
    LOG.debug(f'directory_name: {directory_name}')

    manga_details = filename_parser(filename, logging_info)

    metadata_tagger(file_path, manga_details[0], manga_details[1], manga_details[2], logging_info, manga_details[3],
                    None)

    # Remove manga directory if empty
    try:
        if not any(directory_path.iterdir()) and directory_path != AppSettings.download_dir:
            try:
                LOG.info(f'Deleting {directory_path}...')
                directory_path.rmdir()
            except OSError as e:
                LOG.info("Error: %s : %s" % (directory_path, e.strerror))
    except FileNotFoundError:
        LOG.debug(f'Race condition hit, {directory_name} was already deleted')


def filename_parser(filename, logging_info):
    LOG.info(f'Attempting to rename "{filename}"...', extra=logging_info)

    # Parse the manga title and chapter name/number (this depends on where the manga is downloaded from)
    try:
        if filename.find('-.-') == -1:
            raise UnparsableFilenameError(filename, '-.-')

        filename = filename.split(' -.- ')
        LOG.info(f'Filename was successfully parsed as {filename}.', extra=logging_info)
    except UnparsableFilenameError as ufe:
        LOG.exception(ufe, extra=logging_info)
        return None

    manga_title: str = filename[0]
    chapter_title: str = path.splitext(filename[1].lower())[0]
    manga_title = manga_title.strip()
    LOG.debug(f'manga_title: {manga_title}')
    LOG.debug(f'chapter: {chapter_title}')

    format = "MANGA"
    volume = re.findall(r"(?i)(?:Vol|v|volume)(?:\s|\.)?(?:\s|\.)?([0-9]+(?:\.[0-9]+)?)", chapter_title)
    if volume:
        volume = volume[0]
    else:
        volume = None

    chapter = re.findall(
        r"(?i)(?:(?:ch|chapter|c)(?:\s|\.)?(?:\s|\.)?(?:([0-9]+(?:\.[0-9]+)?)+(?:-([0-9]+(?:\.[0-9]+)?))?))",
        chapter_title)
    if chapter:
        chapter = f"{chapter[0][0]}"
    else:
        chapter = None
    # If "chapter" is in the chapter substring
    try:
        if not hasNumbers(chapter_title):
            if "oneshot" in chapter_title.lower():
                format = "ONE_SHOT"
            chapter_title = "chap000"

        if "prologue" in chapter_title.lower():
            chapter_title = chapter_title.replace(' ', '')
            chapter_title = re.sub('^\D*', '', chapter_title)
            chapter_title = "chap000." + chapter_title

        chapter_title = chapter_title.replace(' ', '')
        chapter_title = re.sub('\(\d*\)$', '', chapter_title)
        # Remove (1) (2) .. because it's often redundant and mess with parsing
        chapter_title = re.sub('\D*$', '', chapter_title)
        # Removed space and any character at the end of the chapter_title that are not number. Usually that's the name of the chapter.

        # Match "Chapter5" "GAME005" "Page/005" "ACT-50" "#505" "V05.5CHAP5.5" without the chapter number, we removed spaces above
        chapter_title_pattern = "[^\d\.]\D*\d*[.,]?\d*[^\d\.]\D*"

        if re.match(chapter_title_pattern, chapter_title):
            p = re.compile(chapter_title_pattern)
            prog = p.match(chapter_title)
            chapter_title_name = prog.group(0)
            delimiter = chapter_title_name
            delimiter_index = len(chapter_title_name)
        else:
            raise UnparsableFilenameError(filename, 'ch/chapter')
    except UnparsableFilenameError as ufe:
        LOG.exception(ufe, extra=logging_info)
        return None

    LOG.debug(f'delimiter: {delimiter}')
    LOG.debug(f'delimiter_index: {delimiter_index}')

    i = chapter_title.index(delimiter) + delimiter_index
    LOG.debug(f'Iterator i: {i}')
    LOG.debug(f'Length: {len(chapter_title)}')

    chapter_number = ''
    while i < len(chapter_title):
        substring = chapter_title[i]
        LOG.debug(f'substring: {substring}')

        if substring.isdigit() or substring == '.':
            chapter_number += chapter_title[i]
            i += 1

            LOG.debug(f'chapter_number: {chapter_number}')
            LOG.debug(f'Iterator i: {i}')
        else:
            break

    if chapter_number.find('.') == -1:
        chapter_number = chapter_number.zfill(3)
    else:
        chapter_number = chapter_number.zfill(5)

    LOG.debug(f'chapter_number: {chapter_number}')

    logging_info['chapter_number'] = chapter_number
    if chapter is not None:
        return manga_title, chapter, format, volume
    else:
        return manga_title, chapter_number, format, volume


def rename_action(current_file_path: Path, new_file_path: Path, manga_title, chapter_number, logging_info):
    chapter_number = chapter_number.replace('.', '-')
    results = ProcFilesTable.search(manga_title, chapter_number)
    LOG.debug(f'Results: {results}')

    # If the series OR the chapter has not been processed
    if results is None:
        LOG.info(f'"{manga_title}" chapter {chapter_number} has not been processed before. '
                 f'Proceeding with file rename...', extra=logging_info)
        shutil.move(current_file_path, new_file_path)
        LOG.info(f'"{new_file_path.name.strip(".cbz")}" has been renamed.', extra=logging_info)
        ProcFilesTable.insert_record(current_file_path, new_file_path, manga_title, chapter_number,
                                     logging_info)
    else:
        versions = ['v2', 'v3', 'v4', 'v5']

        existing_old_filename = results['old_filename']
        existing_current_filename = results['new_filename']

        # Check to see if the file exists at the old location before doing anything rash like deleting the new file.
        if not path.exists(existing_current_filename):
            LOG.info(f'"{manga_title}" chapter {chapter_number} has been processed before but does not exist'
                     f' at target location. Proceeding with file rename...', extra=logging_info)
            shutil.move(current_file_path, new_file_path)
            LOG.info(f'"{new_file_path.name.strip(".cbz")}" has been renamed.', extra=logging_info)
            ProcFilesTable.insert_record(current_file_path, new_file_path, manga_title, chapter_number,
                                         logging_info)

        # If currently processing file has the same name as an existing file
        if existing_current_filename == new_file_path.name:
            # If currently processing file has a version in its filename
            if any(version in current_file_path.name.lower() for version in versions):
                # If the version is newer than the existing file
                if compare_versions(existing_old_filename, current_file_path.name):
                    LOG.info(f'Newer version of "{manga_title}" chapter {chapter_number} has been found. Deleting '
                             f'existing file and proceeding with file rename...', extra=logging_info)
                    new_file_path.unlink()
                    LOG.info(f'"{new_file_path.name}" has been deleted! Proceeding to rename new file...',
                             extra=logging_info)
                    shutil.move(current_file_path, new_file_path)
                    LOG.info(f'"{new_file_path.name.strip(".cbz")}" has been renamed.', extra=logging_info)
                    ProcFilesTable.update_record(results, current_file_path, new_file_path, logging_info)
                else:
                    LOG.warning(f'"{current_file_path.name}" was not renamed due being the exact same as the '
                                f'existing chapter; file currently being processed will be deleted',
                                extra=logging_info)
                    current_file_path.unlink()
                    raise FileUpdateNotRequiredError(current_file_path.name)
            # If the current file doesn't have a version in it's filename, but the existing file does
            elif any(version in existing_old_filename.lower() for version in versions):
                LOG.warning(f'"{current_file_path.name}" was not renamed due to not being an updated version '
                            f'of the existing chapter; file currently being processed will be deleted',
                            extra=logging_info)
                current_file_path.unlink()
                raise FileUpdateNotRequiredError(current_file_path.name)
            # If all else fails
            else:
                LOG.warning(f'No changes have been found for "{existing_current_filename}"; file currently being '
                            f'Skipping', extra=logging_info)
                #current_file_path.unlink()
                raise FileAlreadyProcessedError(current_file_path.name)

    LOG.info(f'"{new_file_path.name}" will be unlocked for any pending processes.', extra=logging_info)
    CURRENTLY_PENDING_RENAME.remove(str(new_file_path))


def compare_versions(old_filename: str, new_filename: str):
    old_version = 0
    new_version = 0

    LOG.debug('Preprocessing')
    LOG.debug(f'Old Version: {old_version}')
    LOG.debug(f'New Version: {new_version}')

    if 'v2' in old_filename.lower():
        old_version = 2
    elif 'v3' in old_filename.lower():
        old_version = 3
    elif 'v4' in old_filename.lower():
        old_version = 4
    elif 'v5' in old_filename.lower():
        old_version = 5

    if 'v2' in new_filename.lower():
        new_version = 2
    elif 'v3' in new_filename.lower():
        new_version = 3
    elif 'v4' in new_filename.lower():
        new_version = 4
    elif 'v5' in new_filename.lower():
        new_version = 5

    LOG.debug('Postprocessing')
    LOG.debug(f'Old Version: {old_version}')
    LOG.debug(f'New Version: {new_version}')

    if new_version > old_version:
        return True
    else:
        return False


def search_the_api(manga_title, anilist_id, format, isadult, logging_info):
    """Return an Anilist with full search results

    Searches the API using the Manga_title or anilist_id
    """
    if anilist_id is not None:
        LOG.info(f'Searching Anilist for ID: "{anilist_id}"')
        return AniList.search_details_by_series_id(anilist_id, format, logging_info)
    else:
        LOG.info(f'Searching Anilist for Title: "{manga_title}"')
        return AniList.search_details_by_series_title( manga_title, isadult, format, logging_info)


def metadata_tagger(file_path, manga_title, manga_chapter_number, format, logging_info, volume, anilist_id=None):
    """Return a Metadata Object

    Checks to see if the manga title is in the exceptions list
    Searches the metadata db to see if the manga metadata exists
    If not searches the api to see if the manga metadata exists and adds the metadata to the database
    Moves the file to the destination folder
    Adds comicinfo and cover.
    """

    isadult = AppSettings.adult_result

    if Path(f'{AppSettings.data_dir}/exceptions.json').exists():
        with open(f'{AppSettings.data_dir}/exceptions.json', 'r') as exceptions_json:
            exceptions = json.load(exceptions_json)
        if manga_title in exceptions:
            LOG.info('Manga_title found in exceptions.json, using manga specific configuration...', extra=logging_info)
            if exceptions[manga_title]['format'] == "MANGA" or exceptions[manga_title]['format'] == "ONE_SHOT":
                format = exceptions[manga_title]['format']
            if exceptions[manga_title]['adult'] is True or exceptions[manga_title]['adult'] is False:
                isadult = exceptions[manga_title]['adult']
            if "anilist_id" in exceptions[manga_title]:
                anilist_id = exceptions[manga_title]['anilist_id']
            if "anilist_title" in exceptions[manga_title]:
                manga_title = exceptions[manga_title]['anilist_title']

    LOG.info(f'Table search value is "{manga_title}"', extra=logging_info)
    if manga_title in SKIP_MANGA:
        LOG.info(f'Skipping "{manga_title}". API found nothing when last attempted'
                 f'Try adding an exception to the exception.json list.', extra=logging_info)
        return None
    # if manga_title in CURRENTLY_PENDING_API_REQUEST:
    #     LOG.info(f'A Search is currently being made for "{manga_title}". Locking API '
    #              f'Until this lookup is complete...', extra=logging_info)
    #
    #     while manga_title in CURRENTLY_PENDING_API_REQUEST:
    #         time.sleep(1)

    if anilist_id is not None:
        LOG.info('Searching manga_metadata for anilist id.', extra=logging_info)
        meta_table_details = MetadataTable.search_by_search_id(anilist_id)
    else:
        LOG.info('Searching manga_metadata for manga title by search value...', extra=logging_info)
        meta_table_details = MetadataTable.search_by_search_value(manga_title)

    if meta_table_details is None:  # The manga is not in the database, so ping the API and create the database
        # CURRENTLY_PENDING_API_REQUEST.add(manga_title)
        LOG.info('Manga was not found in the database; resorting to Anilist API.', extra=logging_info)
        try:
            anilist_details = search_the_api(manga_title, anilist_id, format, isadult, logging_info)
        except AniListRateLimit as e:
            LOG.warning(e, extra=logging_info)
            LOG.warning('Manga Tagger has unintentionally breached the API limits on Anilist. Waiting 60s to clear '
                        'all rate limiting limits...')
            time.sleep(60)
            anilist_details = search_the_api(manga_title, anilist_id, format, isadult, logging_info)
        if anilist_details is None and isadult:
            LOG.info(f'Re-attempting API Lookup "{manga_title}" without the adult tag set.', extra=logging_info)
            try:
                anilist_details = search_the_api(manga_title, anilist_id, format, False, logging_info)
            except AniListRateLimit as e:
                LOG.warning(e, extra=logging_info)
                LOG.warning('Manga Tagger has unintentionally breached the API limits on Anilist. Waiting 60s to clear '
                            'all rate limiting limits...')
                time.sleep(60)
                anilist_details = search_the_api(manga_title, anilist_id, format, False, logging_info)
        if anilist_details is None:
            LOG.info(f'API Lookup failed for "{manga_title}" Unlocking...', extra=logging_info)
            # CURRENTLY_PENDING_API_REQUEST.remove(manga_title)
            SKIP_MANGA.add(manga_title)
            raise MangaNotFoundError(manga_title)

        try:
            add_to_metadata_table(manga_title, anilist_details, logging_info)
        except:
            LOG.info(f'Database Insert failed with API for "{manga_title}" Unlocking...', extra=logging_info)
            # CURRENTLY_PENDING_API_REQUEST.remove(manga_title)
            raise()
        LOG.info(f'API Lookup Succeeded for "{manga_title}" Unlocking...', extra=logging_info)
        # CURRENTLY_PENDING_API_REQUEST.remove(manga_title)

        meta_table_details = MetadataTable.search_by_search_value(manga_title)
        first = True

    series_title = meta_table_details.get('series_title')
    series_title_legal = slugify(series_title)
    manga_library_dir = Path(AppSettings.library_dir, series_title_legal)
    #TODO take in a template for new filename
    try:
        if volume is not None:
            new_filename = f"{series_title_legal} Vol.{volume} Ch.{manga_chapter_number}.cbz"
        else:
            new_filename = f"{series_title_legal} Ch.{manga_chapter_number}.cbz"
        LOG.debug(f'new_filename: {new_filename}')
    except TypeError:
        LOG.warning(f'Manga Tagger was unable create a filename using "{series_title_legal}"', extra=logging_info)
        return None
    new_file_path = Path(manga_library_dir, new_filename)
    
    if AppSettings.mode_settings is None or AppSettings.mode_settings['rename_file']:
        if not manga_library_dir.exists():
            LOG.info(
                f'A directory for "{series_title}" in "{AppSettings.library_dir}" does not exist; creating now.')
            try:
                manga_library_dir.mkdir()
            except FileExistsError:
                LOG.debug(f'Race event happened on Folder Creation: {manga_library_dir}')
        try:
            # Multithreading Optimization
            if str(new_file_path) in CURRENTLY_PENDING_RENAME:
                LOG.info(f'A file is currently being renamed under the filename "{new_filename}". Locking '
                         f'{file_path} from further processing until this rename action is complete...',
                         extra=logging_info)

                while str(new_file_path) in CURRENTLY_PENDING_RENAME:
                    time.sleep(1)

                LOG.info(f'The file being renamed to "{new_file_path}" has been completed. Unlocking '
                         f'"{new_filename}" for file rename processing.', extra=logging_info)
            else:
                LOG.info(f'No files currently currently being processed under the filename '
                         f'"{new_filename}". Locking new filename for processing...', extra=logging_info)
                CURRENTLY_PENDING_RENAME.add(str(new_file_path))

            rename_action(file_path, new_file_path, series_title, manga_chapter_number, logging_info)
        except (FileExistsError, FileUpdateNotRequiredError, FileAlreadyProcessedError) as e:
            LOG.exception(e, extra=logging_info)
            CURRENTLY_PENDING_RENAME.remove(str(new_file_path))
            return

    if manga_title in ProcSeriesTable.processed_series:
        LOG.info(f'Found an entry in manga_metadata for "{manga_title}".', extra=logging_info)
    else:
        LOG.info(f'Found an entry in manga_metadata for "{manga_title}"; unlocking series for processing.',
                 extra=logging_info)
        ProcSeriesTable.processed_series.add(manga_title)

    manga_metadata = Metadata(manga_title, logging_info, details=meta_table_details)
    logging_info['metadata'] = manga_metadata.__dict__

    if AppSettings.mode_settings is None or ('write_comicinfo' in AppSettings.mode_settings.keys()
                                             and AppSettings.mode_settings['write_comicinfo']):
        if AppSettings.image:
            if not Path(f'{AppSettings.image_dir}/{series_title_legal}_cover.jpg').exists():
                LOG.info(f'Image directory configured but cover not found. Downloading series cover image...'
                         , extra=logging_info)
                if anilist_details is None:
                    anilist_details = search_the_api(manga_title, meta_table_details.get("_id"), format, isadult,
                                                     logging_info)
                download_cover_image(series_title_legal, anilist_details['coverImage']['extraLarge'])
            else:
                LOG.debug('Series cover image already exist, not downloading.', extra=logging_info)
        comicinfo_xml = construct_comicinfo_xml(manga_metadata, manga_chapter_number, logging_info, volume)
        reconstruct_manga_chapter(comicinfo_xml, new_file_path, logging_info)
        if AppSettings.image and (not AppSettings.image_first or (AppSettings.image_first 
                                                                  and int(float(manga_chapter_number))==1)):
            add_cover_to_manga_chapter(series_title_legal, new_file_path, logging_info)
    
    LOG.info(f'Processing on "{new_file_path}" has finished.', extra=logging_info)
    return manga_metadata


def add_to_metadata_table(manga_title, anilist_details, logging_info):
    '''
    Completes a Full API Search using the
    Downloads the
    '''
    series_id = anilist_details['id']
    LOG.info(f'Manga: "{manga_title}" found on Anilist with ID:"{series_id} ".', extra=logging_info)
    LOG.debug(f'anilist_details: {anilist_details}')

    manga_metadata = Metadata(manga_title, logging_info, anilist_details)
    logging_info['metadata'] = manga_metadata.__dict__

    if AppSettings.mode_settings is None or ('database_insert' in AppSettings.mode_settings.keys()
                                             and AppSettings.mode_settings['database_insert']):
        if MetadataTable.search_by_search_id(series_id):
            # TODO use push instead of set, issue may occur with race conditions.
            MetadataTable.update({"_id":series_id}, {"$set": {'search_value': manga_title}}, logging_info)
            LOG.info(f'Updating metadata table for "{manga_title}" found on Anilist API; ', extra=logging_info)
            return

        MetadataTable.insert(manga_metadata, logging_info)
        LOG.info(f'Inserted metadata for "{manga_title}" found on Anilist API; ', extra=logging_info)
    return


def construct_anilist_titles(anilist_details):
    anilist_titles = {}

    if anilist_details['romaji'] is not None:
        anilist_titles['romaji'] = anilist_details['romaji']

    if anilist_details['english'] is not None:
        anilist_titles['english'] = anilist_details['english']

    if anilist_details['native'] is not None:
        anilist_titles['native'] = anilist_details['native']

    if anilist_details['synonyms'] is not None:
        anilist_titles['synonyms'] = anilist_details['synonyms']

    return anilist_titles


def construct_comicinfo_xml(metadata: Metadata, chapter_number, logging_info, volume_number):
    LOG.info(f'Constructing comicinfo object for "{metadata.series_title}", chapter {chapter_number}...',
             extra=logging_info)

    comicinfo = Element('ComicInfo')

    application_tag = Comment('Generated by Manga Tagger, an Endless Galaxy Studios project')
    comicinfo.append(application_tag)

    series = SubElement(comicinfo, 'Series')
    series.text = metadata.series_title

    if metadata.series_title_eng is not None and compare(metadata.series_title,
                                                         metadata.series_title_eng) != 1 and metadata.series_title_eng != "":
        localized_series = SubElement(comicinfo, 'LocalizedSeries')
        localized_series.text = metadata.series_title_eng

    number = SubElement(comicinfo, 'Number')
    number.text = f'{chapter_number}'
    if volume_number is not None:
        volume = SubElement(comicinfo, 'Volume')
        volume.text = f'{volume_number}'

    if metadata.volumes is not None:
        count = SubElement(comicinfo,"Count")
        count.text = f'{metadata.volumes}'

    if metadata.description is None:
        metadata.description = ""
    summary = SubElement(comicinfo, 'Summary')
    soup = BeautifulSoup(metadata.description, "html.parser")
    summary.text = soup.get_text()

    publish_date = datetime.strptime(metadata.publish_date, '%Y-%m-%d').date()
    year = SubElement(comicinfo, 'Year')
    year.text = f'{publish_date.year}'

    month = SubElement(comicinfo, 'Month')
    month.text = f'{publish_date.month}'

    writer = SubElement(comicinfo, 'Writer')
    writer.text = next(iter(metadata.staff['story']))

    penciller = SubElement(comicinfo, 'Penciller')
    penciller.text = next(iter(metadata.staff['art']))

    inker = SubElement(comicinfo, 'Inker')
    inker.text = next(iter(metadata.staff['art']))

    colorist = SubElement(comicinfo, 'Colorist')
    colorist.text = next(iter(metadata.staff['art']))

    letterer = SubElement(comicinfo, 'Letterer')
    letterer.text = next(iter(metadata.staff['art']))

    cover_artist = SubElement(comicinfo, 'CoverArtist')
    cover_artist.text = next(iter(metadata.staff['art']))

    #    publisher = SubElement(comicinfo, 'Publisher')
    #    publisher.text = next(iter(metadata.serializations))

    genre = SubElement(comicinfo, 'Genre')
    for mg in metadata.genres:
        if genre.text is not None:
            genre.text += f',{mg}'
        else:
            genre.text = f'{mg}'

    web = SubElement(comicinfo, 'Web')
    web.text = metadata.anilist_url

    language = SubElement(comicinfo, 'LanguageISO')
    language.text = 'en'

    manga = SubElement(comicinfo, 'Manga')
    manga.text = 'Yes'

    notes = SubElement(comicinfo, 'Notes')
    notes.text = f'Scraped metadata from AniList on {metadata.scrape_date}'

    comicinfo.set('xmlns:xsd', 'http://www.w3.org/2001/XMLSchema')
    comicinfo.set('xmlns:xsi', 'http://www.w3.org/2001/XMLSchema-instance')

    LOG.info(f'Finished creating ComicInfo object for "{metadata.series_title}", chapter {chapter_number}.',
             extra=logging_info)
    return parseString(tostring(comicinfo)).toprettyxml(indent="   ")


def reconstruct_manga_chapter(comicinfo_xml, manga_file_path, logging_info):
    if not path.exists(manga_file_path):
        LOG.Warning(f'{manga_file_path} was not created, cannot add the ComicInfo.xml file')
        return
    try:
        with ZipFile(manga_file_path, 'a') as zipfile:
            zipfile.writestr('ComicInfo.xml', comicinfo_xml)
    except Exception as e:
        LOG.exception(e, extra=logging_info)
        LOG.warning('Manga Tagger is unfamiliar with this error. Please log an issue for investigation.',
                    extra=logging_info)
        return
    LOG.info(f'ComicInfo.xml has been created and appended to "{manga_file_path}".', extra=logging_info)


def add_cover_to_manga_chapter(manga_title, manga_file_path, logging_info):
    if not path.exists(manga_file_path):
        LOG.Warning(f'{manga_file_path} was not created, cannot add the Cover image')
        return
    try:
        with ZipFile(manga_file_path, 'a') as zipfile:
            if AppSettings.image and Path(f'{AppSettings.image_dir}/{manga_title}_cover.jpg').exists():
                zipfile.write(f'{AppSettings.image_dir}/{manga_title}_cover.jpg', '000_cover.jpg')
    except Exception as e:
        LOG.exception(e, extra=logging_info)
        LOG.warning('Manga Tagger is unfamiliar with this error. Please log an issue for investigation.',
                    extra=logging_info)
        return
    LOG.info(f'Cover Image has been added to "{manga_file_path}".', extra=logging_info)


def download_cover_image(manga_title, image_url):
    if AppSettings.image:
        if not Path(f'{AppSettings.image_dir}/{manga_title}_cover.jpg').exists():
            LOG.info('Downloading series cover image...')
            image = requests.get(image_url)
            with open(f'{AppSettings.image_dir}/{manga_title}_cover.jpg', 'wb') as image_file:
                image_file.write(image.content)
        else:
            LOG.debug('Series cover image already exist, not downloading.')
    return


def slugify(value, allow_unicode=False):
    """
    Taken from https://github.com/django/django/blob/master/django/utils/text.py
    Convert to ASCII if 'allow_unicode' is False. Convert spaces or repeated
    dashes to single dashes. Remove characters that aren't alphanumerics,
    underscores, or hyphens. Convert to lowercase. Also strip leading and
    trailing whitespace, dashes, and underscores.
    """
    value = str(value)
    if allow_unicode:
        value = unicodedata.normalize('NFKC', value)
    else:
        value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode('ascii')
    value = re.sub(r'[^\w\s-]', '', value)
    value = value.strip()
    return value


def hasNumbers(inputString):
    return bool(re.search(r'\d', inputString))
