from os import rename
from typing import Any
from classes.prepare_metadata import prepare_metadata
import copy
import csv
import hashlib
import json
import logging
import os
import platform
import re
from datetime import datetime
from itertools import chain, zip_longest, groupby, product
from os.path import dirname as up
from urllib.parse import urlparse
import time
import random
import socket
import psutil
import shutil
from multiprocessing.dummy import Pool as ThreadPool
import ujson

import requests
from requests.adapters import HTTPAdapter
from bs4 import BeautifulSoup
from requests.api import delete

import classes.make_settings as make_settings
import classes.prepare_webhooks as prepare_webhooks
import extras.OFRenamer.start as ofrenamer
import warnings
from multiprocessing import cpu_count
from mergedeep import merge, Strategy


path = up(up(os.path.realpath(__file__)))
os.chdir(path)

warnings.filterwarnings(
    "ignore", message='.*looks like a URL.*', category=UserWarning, module='bs4')

json_global_settings = None
min_drive_space = 0
webhooks = None
max_threads = -1
os_name = platform.system()
proxies = None
cert = None


def setup_logger(name, log_file, level=logging.INFO):
    """To setup as many loggers as you want"""
    log_filename = ".logs/"+log_file
    os.makedirs(os.path.dirname(log_filename), exist_ok=True)
    formatter = logging.Formatter(
        '%(asctime)s %(levelname)s %(name)s %(message)s')

    handler = logging.FileHandler(log_filename, 'w+', encoding='utf-8')
    handler.setFormatter(formatter)

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.addHandler(handler)

    return logger


log_error = setup_logger('errors', 'errors.log')


def assign_vars(config):
    global json_global_settings, min_drive_space, webhooks, max_threads, proxies, cert

    json_config = config
    json_global_settings = json_config["settings"]
    min_drive_space = json_global_settings["min_drive_space"]
    webhooks = json_global_settings["webhooks"]
    max_threads = json_global_settings["max_threads"]
    proxies = json_global_settings["socks5_proxy"]
    cert = json_global_settings["cert"]


def rename_duplicates(seen, filename):
    filename_lower = filename.lower()
    if filename_lower not in seen:
        seen.add(filename_lower)
    else:
        count = 1
        while filename_lower in seen:
            filename = filename+" ("+str(count)+")"
            filename_lower = filename.lower()
            count += 1
        seen.add(filename_lower)
    return [seen, filename]


def parse_links(site_name, input_link):
    if site_name in {"onlyfans", "starsavn"}:
        username = input_link.rsplit('/', 1)[-1]
        return username

    if site_name in {"patreon", "fourchan", "bbwchan"}:
        if "catalog" in input_link:
            input_link = input_link.split("/")[1]
            print(input_link)
            return input_link
        if input_link[-1:] == "/":
            input_link = input_link.split("/")[3]
            return input_link
        if "4chan.org" not in input_link:
            return input_link


def clean_text(string, remove_spaces=False):
    matches = ["\n", "<br>"]
    for m in matches:
        string = string.replace(
            m, " ").strip()
    string = ' '.join(string.split())
    string = BeautifulSoup(string, "lxml").get_text()
    SAFE_PTN = "[^0-9a-zA-Z-_.'()]+"
    string = re.sub(SAFE_PTN, ' ',  string.strip()
                    ).strip()
    if remove_spaces:
        string = string.replace(' ', '_')
    return string


def format_media_set(media_set):
    merged = merge({}, *media_set, strategy=Strategy.ADDITIVE)
    if "directories" in merged:
        for directory in merged["directories"]:
            os.makedirs(directory, exist_ok=True)
        merged.pop("directories")
    return merged
    media_set = list(chain(*media_set))
    media_set.sort(key=lambda x: x["type"])
    media_set = [list(g) for k, g in groupby(
        media_set, key=lambda x: x["type"])]
    new_list = []
    for item in media_set:
        item2 = {k: [d[k] for d in item] for k in item[0]}
        item2["type"] = item2['type'][0].title()
        item2["valid"] = list(chain(*item2["valid"]))
        item2["invalid"] = list(chain(*item2["invalid"]))
        if item2["valid"]:
            seen = set()
            item2["valid"] = [x for x in item2["valid"]
                              if x["filename"] not in seen and not seen.add(x["filename"])]
            seen = set()
            location_directories = [x["directory"] for x in item2["valid"]
                                    if x["directory"] not in seen and not seen.add(x["directory"])]
            for location_directory in location_directories:
                os.makedirs(location_directory+os.sep, exist_ok=True)
            item2["valid"] = [list(g) for k, g in groupby(
                item2["valid"], key=lambda x: x["post_id"])]
        new_list.append(item2)
    return new_list


def format_image(filepath, timestamp):
    if os_name == "Windows":
        from win32_setctime import setctime
        while True:
            try:
                setctime(filepath, timestamp)
            except Exception as e:
                print(filepath)
                continue
            break
    os.utime(filepath, (timestamp, timestamp))


def filter_metadata(datas):
    for key, item in datas.items():
        for items in item["valid"]:
            for item2 in items:
                item2.pop("session")
    return datas


def import_archive(archive_path) -> Any:
    metadata = {}
    if os.path.exists(archive_path) and os.path.getsize(archive_path):
        with open(archive_path, 'r', encoding='utf-8') as outfile:
            metadata = ujson.load(outfile)
    return metadata


def export_archive(datas, archive_path, json_settings, rename=True, legacy_directory=""):
    if os.path.exists(legacy_directory):
        shutil.rmtree(legacy_directory)
    archive_directory = os.path.dirname(archive_path)
    if json_settings["export_metadata"]:
        export_type = json_global_settings["export_type"]
        if export_type == "json":
            os.makedirs(archive_directory, exist_ok=True)
            if os.path.exists(archive_path) and rename:
                datas2 = ofrenamer.start(archive_path, json_settings)
                if datas == datas2:
                    return
            with open(archive_path, 'w', encoding='utf-8') as outfile:
                ujson.dump(datas, outfile, indent=2)
        # if export_type == "csv":
        #     archive_path = os.path.join(archive_directory+".csv")
        #     with open(archive_path, mode='w', encoding='utf-8', newline='') as csv_file:
        #         for data in datas:
        #             fieldnames = []
        #             media_type = data["type"].lower()
        #             valid = list(chain.from_iterable(data["valid"]))
        #             invalid = list(chain.from_iterable(data["invalid"]))
        #             if valid:
        #                 fieldnames.extend(valid[0].keys())
        #             elif invalid:
        #                 fieldnames.extend(invalid[0].keys())
        #             header = [media_type]+fieldnames
        #             if len(fieldnames) > 1:
        #                 writer = csv.DictWriter(csv_file, fieldnames=header)
        #                 writer.writeheader()
        #                 for item in valid:
        #                     writer.writerow({**{media_type: "valid"}, **item})
        #                 for item in invalid:
        #                     writer.writerow({**{media_type: "invalid"}, **item})


def format_paths(j_directories, site_name):
    paths = []
    for j_directory in j_directories:
        format_path = j_directory
        path = format_path.replace("{site_name}", site_name)
        paths.append(path)
    return paths


def reformat(directory, post_id, media_id, filename, text, ext, date, username, file_directory_format, file_name_format, date_format, maximum_length):
    post_id = "" if post_id is None else str(post_id)
    media_id = "" if media_id is None else str(media_id)
    if type(date) is str:
        date = datetime.strptime(
            date, "%d-%m-%Y %H:%M:%S")
    formatted = []
    formats = [file_directory_format, file_name_format]
    for unformatted in formats:
        has_text = False
        if "{text}" in unformatted:
            has_text = True
            text = clean_text(text)
        path = unformatted.replace("{post_id}", post_id)
        path = path.replace("{media_id}", media_id)
        path = path.replace("{username}", username)
        filtered_text = text[:maximum_length]
        directory = directory.replace(text, filtered_text)
        path = path.replace("{text}", filtered_text)
        date2 = date.strftime(date_format)
        path = path.replace("{date}", date2)
        path = path.replace("{file_name}", filename)
        path = path.replace("{ext}", ext)

        if has_text:
            count_string = len(path)
            text_count = len(filtered_text)
            if count_string > maximum_length:
                text_limit = count_string - text_count
                path = path.replace(
                    filtered_text, filtered_text[:text_limit].rstrip())
        formatted.append(path)
    directory2 = os.path.join(directory, *formatted)
    return directory2


def get_directory(directories, site_name):
    directories = format_paths(directories, site_name)
    new_directories = []
    for directory in directories:
        if not os.path.isabs(directory):
            fp = os.path.abspath(".sites")
            x = os.path.join(fp, directory)
            directory = os.path.abspath(x)
        os.makedirs(directory, exist_ok=True)
        new_directories.append(directory)
    directory = check_space(new_directories, min_size=min_drive_space)
    return directory


def check_space(download_paths, min_size=min_drive_space, priority="download"):
    root = ""
    while not root:
        paths = []
        for download_path in download_paths:
            obj_Disk = psutil.disk_usage(download_path)
            free = obj_Disk.free / (1024.0 ** 3)
            x = {}
            x["path"] = download_path
            x["free"] = free
            paths.append(x)
        if priority == "download":
            for item in paths:
                download_path = item["path"]
                free = item["free"]
                if free > min_size:
                    root = download_path
                    break
        elif priority == "upload":
            paths.sort(key=lambda x: x["free"])
            item = paths[0]
            root = item["path"]
    return root


def format_directories(directory, site_name, username, locations=[], api_type=""):
    x = {}
    model_directory = x["model_directory"] = os.path.join(directory, username)
    x["legacy_metadata"] = os.path.join(model_directory, api_type, "Metadata")
    x["metadata_directory"] = os.path.join(model_directory, "Metadata")
    x["api_directory"] = os.path.join(model_directory, api_type)
    x["locations"] = []
    for location in locations:
        directories = {}
        cats = ["Unsorted", "Free", "Paid"]
        for cat in cats:
            cat2 = cat
            if "Unsorted" in cat2:
                cat2 = ""
            path = os.path.join(api_type, cat2, location[0])
            directories[cat.lower()] = path
        y = {}
        y["sorted_directories"] = directories
        y["media_type"] = location[0]
        y["alt_media_type"] = location[1]
        x["locations"].append(y)
    return x


def are_long_paths_enabled():
    if os_name == "Windows":
        from ctypes import WinDLL, c_ubyte
        ntdll = WinDLL('ntdll')

        if hasattr(ntdll, 'RtlAreLongPathsEnabled'):

            ntdll.RtlAreLongPathsEnabled.restype = c_ubyte
            ntdll.RtlAreLongPathsEnabled.argtypes = ()
            return bool(ntdll.RtlAreLongPathsEnabled())

        else:
            return False


def check_for_dupe_file(download_path, content_length):
    found = False
    if os.path.isfile(download_path):
        content_length = int(content_length)
        local_size = os.path.getsize(download_path)
        if local_size == content_length:
            found = True
    return found


def downloader(r, download_path, count=0):
    delete = False
    try:
        with open(download_path, 'wb') as f:
            delete = True
            for chunk in r.iter_content(chunk_size=1024):
                if chunk:  # filter out keep-alive new chunks
                    f.write(chunk)
    except (ConnectionResetError) as e:
        if delete:
            os.unlink(download_path)
        return
    except (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError) as e:
        return
    except Exception as e:
        if delete:
            os.unlink(download_path)
        string = f"{e}\n Tries: {count}"
        log_error.exception(
            string)
        return
    return True


# def restore_missing_data2(master_set2, media_set):
#     count = 0
#     new_set = []
#     for item in media_set:
#         if not item:
#             link_item = master_set2[count]
#             link = link_item["link"]
#             offset = int(link.split('?')[-1].split('&')[1].split("=")[1])
#             limit = int(link.split("?")[-1].split("&")[0].split("=")[1])
#             num = 2
#             x = []
#             limit2 = int(limit/num)
#             offset2 = offset
#             for item in range(1, num+1):
#                 link2 = link.replace("limit="+str(limit), "limit="+str(limit2))
#                 link2 = link2.replace(
#                     "offset="+str(offset), "offset="+str(offset2))
#                 offset2 += limit2
#                 i = {}
#                 i["link"] = link2
#                 i["count"] = link_item["count"]
#                 new_set.append(i)
#                 print(link2)
#             print
#         print(master_set2[count]["link"])
#         count += 1
#     return new_set


def get_config(config_path):
    if os.path.isfile(config_path):
        if os.stat(config_path).st_size > 0:
            json_config = json.load(open(config_path))
        else:
            json_config = {}
    else:
        json_config = {}
    file_name = os.path.basename(config_path)
    if file_name == "config.json":
        json_config2 = json.loads(json.dumps(make_settings.config(
            **json_config), default=lambda o: o.__dict__))
    else:
        if "onlyfans" in json_config:
            new = {}
            new["supported"] = json_config
            json_config = new
        json_config2 = json.loads(json.dumps(make_settings.extra_auth(
            **json_config), default=lambda o: o.__dict__))
    if json_config != json_config2:
        update_config(json_config2, file_name=file_name)
    if not json_config:
        input(
            f"The .settings\\{file_name} file has been created. Fill in whatever you need to fill in and then press enter when done.\n")
        json_config2 = json.load(open(config_path))

    json_config = copy.deepcopy(json_config2)
    return json_config, json_config2


def update_config(json_config, file_name="config.json"):
    directory = '.settings'
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, file_name)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(json_config, f, ensure_ascii=False, indent=2)


def choose_auth(array):
    string = ""
    names = []
    array = [{"auth_count": -1, "username": "All"}]+array
    name_count = len(array)
    if name_count > 1:

        count = 0
        for x in array:
            name = x["username"]
            string += str(count)+" = "+name
            names.append(x)
            if count+1 != name_count:
                string += " | "

            count += 1

    print("Auth Usernames: "+string)
    value = int(input().strip())
    if value:
        names = [names[value]]
    else:
        names.pop(0)
    return names


def choose_option(subscription_list, auto_scrape_names):
    names = subscription_list[0]
    if names:
        print("Names: Username = username | "+subscription_list[1])
        if not auto_scrape_names:
            value = "2"
            value = input().strip()
            if value.isdigit():
                if value == "0":
                    names = names[1:]
                else:
                    names = [names[int(value)]]
            else:
                names = [name for name in names if value in name[1]]
        else:
            value = 0
            names = names[1:]
    return names


def process_names(module, subscription_list, auto_scrape_names, json_auth_array, session_array, json_config, site_name_lower, site_name):
    names = choose_option(
        subscription_list, auto_scrape_names)
    if not names:
        print("There's nothing to scrape.")
        return
    app_token = ""
    for name in names:
        # Extra Auth Support
        auth_count = name[0]
        api = session_array[auth_count]
        name = name[-1]
        assign_vars(json_config)
        username = parse_links(site_name_lower, name)
        result = module.start_datascraper(
            api, username, site_name)


def process_downloads(apis, module):
    for api in apis:
        subscriptions = api.get_subscriptions(refresh=False)
        for subscription in subscriptions:
            download_info = subscription.download_info
            if download_info:
                module.download_media(api, subscription)
                delete_empty_directories(
                    download_info["model_directory"])
                send_webhook(subscription)


def is_me(user_api):
    if "email" in user_api:
        return True
    else:
        return False


def update_metadata(path, metadata):
    with open(path, 'w') as outfile:
        json.dump(metadata, outfile)
    print


def grouper(n, iterable, fillvalue=None):
    args = [iter(iterable)] * n
    return list(zip_longest(fillvalue=fillvalue, *args))


def create_link_group(max_threads):
    x = range
    print


def legacy_metadata(directory):
    if os.path.exists(directory):
        items = os.listdir(directory)
        matches = ["desktop.ini"]
        metadatas = []
        items = [x for x in items if x not in matches]
        if items:
            for item in items:
                path = os.path.join(directory, item)
                metadata = json.load(open(path))
                metadatas.append(metadata)
                print
        print


def metadata_fixer(directory):
    archive_file = os.path.join(directory, "archive.json")
    metadata_file = os.path.join(directory, "Metadata")
    if os.path.exists(archive_file):
        os.makedirs(metadata_file, exist_ok=True)
        new = os.path.join(metadata_file, "Archive.json")
        shutil.move(archive_file, new)


def send_webhook(item):
    download_info = item.download_info
    if download_info["webhook"]:
        for webhook_link in webhooks:
            message = prepare_webhooks.discord()
            embed = message.embed()
            embed.title = f"Downloaded: {item.username}"
            embed.add_field("username", item.username)
            embed.add_field("post_count", item.postsCount)
            embed.add_field("link", item.link)
            embed.image.url = item.avatar
            message.embeds.append(embed)
            message = json.loads(json.dumps(
                message, default=lambda o: o.__dict__))
            x = requests.post(webhook_link, json=message)


def find_between(s, start, end):
    x = (s.split(start))[1].split(end)[0]
    return x


def delete_empty_directories(directory):
    def start(directory):
        for root, dirnames, files in os.walk(directory, topdown=False):
            for dirname in dirnames:
                full_path = os.path.realpath(os.path.join(root, dirname))
                if not os.listdir(full_path):
                    os.rmdir(full_path)
    x = start(directory)
    if os.path.exists(directory):
        if not os.listdir(directory):
            os.rmdir(directory)


def multiprocessing():
    if max_threads < 1:
        pool = ThreadPool()
    else:
        pool = ThreadPool(max_threads)
    return pool


def module_chooser(domain, json_sites):
    string = "Site: "
    site_names = []
    wl = ["onlyfans"]
    bl = ["patreon"]
    site_count = len(json_sites)
    count = 0
    for x in json_sites:
        if not wl:
            if x in bl:
                continue
        elif x not in wl:
            continue
        string += str(count)+" = "+x
        site_names.append(x)
        if count+1 != site_count:
            string += " | "

        count += 1
    string += "x = Exit"
    if domain and domain not in site_names:
        string = f"{domain} not supported"
        site_names = []
    return string, site_names
