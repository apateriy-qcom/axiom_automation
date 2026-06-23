#!/usr/bin/env python
# ---------------------------------------------------------------
# Copyright (c) 2015 QUALCOMM Technologies, Incorporated.
# All Rights Reserved. QUALCOMM Proprietary and Confidential.
# ---------------------------------------------------------------

#
# program:: axiom_launcher.python
#
# This provides a command-line interface to the axiom 'start_test' REST
# Interface and provides monitoring of launched jobs
#
# Usage:
# ------
#
# Here's how to invoke a REST call to axiom using this script
# ::
#
#     axiom_launcher.py
#       --software-product <Software Product>
#       --meta-build-path <Path to metabuild>
#       --build-variant <Build Variant/Client ID>
#       --purpose <Build Purpose>
#       --ci-descriptor <Branch/Repo Name>
#       --override-build-path <path to image build>
#       --override-build-id <build id>
#       --override-build-type <build override type>
#       --override-build-mapping <build mapping>
#       --ci-contacts <email list for Repo>
#       --submitter <user id of user submitting changes to be tested>
#
# Example: Submit a meta-build to test
#
#     axiom_launcher.py
#         --software-product MSM8996.LA.1.0.c2
#         --meta-build-path \\snowcone\builds689\PROD\MSM8996.LA.1.0.c2-01035-STD.PROD-1.39905.2
#         --build-variant STD.PROD
#         --purpose release
#         --ci-descriptor MSM8996.LA.1.0.1
#         --ci-framework Aris
#         --submitter sahill
#
# Example: Submit a meta-build plus an image override to test
#
#     axiom_launcher.py
#         --software-product MSM8996.LA.1.0.1
#         --meta-build-path \\snowcone\builds774\MSM8996.LA.1.0.1-01500-STD.INT-1
#         --build-variant STD.INT
#         --purpose preflight
#         --ci-descriptor LA.HB.1.3.1
#         --ci-framework Lint
#         --override-build-path \\mister\blat\component_model_mandatory_testing\19312016\msm8996
#         --override-build-id av-userspace.lnx.1.0-00060
#         --override-build-type image
#         --override-build-mapping apps
#         --submitter sahill
#
# Exit Codes:
#    0 - Jobs were launched and completed. (regardless if the outcome was pass or fail)
#    1 - No Job triggers are defined in axiom for the specified SP + Purpose + branch.
#    2 - Jobs were aborted

from __future__ import print_function
import argparse
import ast
import logging
import json
import re
import ntpath
import os
import subprocess as sub
import sys
import time
import site
import subprocess
import requests
import warnings
import signal
site.addsitedir(os.path.dirname(os.path.abspath(__file__)))

from notification.libs.email_notifiers import Email  # noqa: E402

logger = logging.getLogger(__name__)

MAX_RETRIES = 7
SLEEP_DURATION = 300  # 300 = 5 minutes
axiom_BASE_API = 'https://api.axiom.qualcomm.com/rest/'
JOB_API = 'exemgr/jobs/{}'
RESULTS_API = 'exemgr/jobs/{}/results'
PLAYLIST_CASES_API = 'test/playlistgroups/{}/?expand=playlists,playlists__playlistcases'
linthyd_paths = ['hyd_archive_au_3', 'hyd_archive_au_4', 'hyd_archive_au_5', \
                 'hyd_archive_au_7', 'hyd_au_scratch_1', 'hyd_au_scratch_2', \
                 'hyd_au_scratch_3', 'hyd_au_scratch_4', \
                 'hyd_archive_au_scratch_1', 'hyd_archive_au_scratch_2', \
                 'hyd_archive_au_scratch_3', 'hyd_archive_au_sractch_3', \
                 'hyd_archive_au_scratch_9']

upagrah_paths = ['hyd_au_scratch']

# ── OAuth helper (Apigee public API) ──────────────────────────────────────
import base64 as _base64
import uuid   as _uuid

def _load_dotenv(path='.env'):
    import os
    try:
        for raw in open(path):
            raw = raw.strip()
            if not raw or raw.startswith('#') or '=' not in raw:
                continue
            k, v = raw.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip().strip(chr(39)).strip(chr(34)))
    except FileNotFoundError:
        pass

def _get_oauth_token():
    import os, json
    from urllib.request import Request, urlopen
    _load_dotenv()
    cid = os.environ.get('AXIOM_CLIENT_ID','')
    cs  = os.environ.get('AXIOM_CLIENT_SECRET','')
    if not cid or not cs:
        logger.warning('AXIOM_CLIENT_ID/SECRET not set — skipping OAuth')
        return None
    b64 = _base64.b64encode(f'{cid}:{cs}'.encode()).decode()
    req = Request(
        url='https://api-int.qualcomm.com/ent/oauth/v1/accesstoken?grant_type=client_credentials',
        method='POST',
        headers={'Authorization': f'Basic {b64}',
                 'Content-Type': 'application/x-www-form-urlencoded'}
    )
    try:
        with urlopen(req, timeout=30) as r:
            return json.loads(r.read())['access_token']
    except Exception as exc:
        logger.warning('OAuth token fetch failed: %s', exc)
        return None

_OAUTH_TOKEN = None   # populated lazily on first submit

PUBLIC_API_BASE = 'https://api-int.qualcomm.com/axiom/v1/public'

def _public_headers():
    return {
        'Authorization': f'Bearer {_OAUTH_TOKEN}',
        'X-QCOM-TracingID': _uuid.uuid4().hex,
        'X-QCOM-AppName': 'AxiomLaunchMonitor',
        'X-QCOM-TokenType': 'OAuth',
        'X-QCOM-ClientType': 'automation-script',
        'Content-Type': 'application/json',
    }

mudpie_paths = ['lv_archive_au_scratch_1', 'sd_au_scratch_5']

# Create a global dictionary tracking state.
# This is necessary for Ctl+C handling
monitor_jobs = dict()
args = None

def process_arguments():
    parser = argparse.ArgumentParser(description='Wrapper for BAIT to send a JSON formatted request to axiom Test Service.')

    parser.add_argument('--axiom-url',
                        dest='axiomurl',
                        default='https://api-int.qualcomm.com/axiom/v1/public/events',
                        help='Base URL to the axiom REST API.')

    parser.add_argument('--monitor-event-id',
                        dest='monitorid',
                        help='Provides ID to monitor sub-axiom job status.')

    parser.add_argument('--blocking-mode',
                        dest='blockingmode',
                        type=int,
                        default=1,
                        help='Provides a parameter to specify testing is in blocking mode or not')

    parser.add_argument('--software-product',
                        dest='softwareProduct',
                        required=True,
                        help='Software Product. Ex: MSM8996.LA.1.0')

    parser.add_argument('--chipset',
                        dest='chipset',
                        help='Chipset. Ex: \'MSM8996\'')

    parser.add_argument('--component',
                        dest='branchcomponent',
                        default='',
                        help='Name of component. i.e. atel-prop.lnx.3.0')
    parser.add_argument('--branch',
                        dest='branch',
                        default='',
                        help='name of branch. i.e. LA.UM.7.1')

    parser.add_argument('--scopes',
                        dest='componentscopes',
                        help='scopes for mult- component.')

    parser.add_argument('--nickname',
                        dest='nickname',
                        help='Provides a parameter to specify a nickname to a specific product. i.e. Redstone2, Marshmallow, Atlas, etc...')

    parser.add_argument('--meta-build-path',
                        dest='metabuildpath',
                        required=True,
                        help='A file:// path to the meta-build to load.')

    parser.add_argument('--enable-metacheck',
                        dest='enable_metacheck',
                        action="store_true",
                        default=False,
                        help='enable meta check before triggering axiom job')

    parser.add_argument('--build-variant',
                        dest='buildVariant',
                        help='A build variant. Also known as Client ID in Tiberium')

    parser.add_argument('--isgating',
                        dest='isgating',
                        action="store_true",
                        default=False,
                        help='isgating')

    parser.add_argument('--override-build-path',
                        dest='buildpath',
                        help='A file:// path to the image build to load.')

    parser.add_argument('--override-secondary-build-path',
                        dest='secondarybuildpath',
                        help='A file:// path to the image build to load.')

    parser.add_argument('--override-build-id',
                        dest='buildId',
                        help='A file:// path to the image build to load.')

    parser.add_argument('--override-build-type',
                        dest='buildtype',
                        default='image',
                        help='The build override type. i.e. image, component, driver.')

    parser.add_argument('--override-build-mapping',
                        dest='buildMapping',
                        default='apps',
                        help='Image build mapping used in meta-build content.xml to identify where the image goes.')

    parser.add_argument('--override-secondary-build-mapping',
                        dest='secondarybuildMapping',
                        default='apps',
                        help='Image build mapping used in meta-build content.xmlto identify where the image goes.')

    parser.add_argument('--purpose',
                        dest='purpose',
                        help='Build/test purpose.')

    parser.add_argument('--parent-axiom-json',
                        dest='parentaxiomJson',
                        help='Needed for build bisection feature. Path to the json with the parent axiom job ids that must be re-run with only the failures')

    parser.add_argument('--ci-descriptor',
                        dest='ciDescriptor',
                        help='Unique name of CI controlled SW asset.')

    parser.add_argument('--ci-framework',
                        dest='ciFramework',
                        required=True,
                        help='Name of CI System. i.e. PW, ARIS, Lint, WPCI')

    parser.add_argument('--ci-job',
                        dest='ciJob',
                        type=int)

    parser.add_argument('--ci-job-url',
                        dest='ciJobURL',
                        help='URL to CI Job')

    parser.add_argument('--ci-job-id',
                        dest='ciJobId',
                        help='CI JobId')

    parser.add_argument('--ci-job-tag',
                        dest='ciJobTAG',
                        help='String where we can pass information as Tags against a CI job')

    parser.add_argument('--ci-contacts',
                        dest='ciContacts',
                        default='lnxbuild',
                        help='email address associated with the CI repo.')

    parser.add_argument('--submitter',
                        dest='submitter',
                        required=True,
                        help='User ID of the user submitting the build.')

    parser.add_argument('--retry-interval',
                        dest='pollingInterval',
                        type=int,
                        default=480,   # 8 Minutes
                        help='Polling interval in seconds.defaut:10 minutes.')

    parser.add_argument('--refresh-rate',
                        dest='refreshrate',
                        type=int,
                        default=30,   # 30 Seconds
                        help='refresh rate for monitoring Axiom.def:30secs')

    parser.add_argument('--retry-cnt',
                        dest='retrycnt',
                        type=int,
                        default=MAX_RETRIES,
                        help='retry times,defaut:3.')

    parser.add_argument('--abort-jobs-on-failure',
                        dest='abort_jobs_on_failure',
                        action="store_true",
                        help='Option will automatically abort pending \
                              axiom jobs if multiple axiom jobs were \
                              spawned by a request and one of those \
                              test jobs failed.')

    parser.add_argument('--just-print',
                        dest='justPrint',
                        action="store_true",
                        help='Just print what would happen but does not call axiom')

    parser.add_argument('--supress-ec',
                        dest='supressEC',
                        action="store_true",
                        help='Supresses any calls to ectool for setting properties')

    parser.add_argument('--site', default='SD',
                        help='Site in which IFR need to run')

    return parser.parse_args()

def Update_EC_Properties(property_key, property_value, supress_calls_to_ECTool):
    args = ("ectool", "setProperty", property_key, property_value)
    logger.info(args)

    if supress_calls_to_ECTool is None or not supress_calls_to_ECTool:
        popen = subprocess.Popen(args, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE,
                                 universal_newlines=True)
        (std_out, std_err) = popen.communicate()
        logger.info('std output  : %s' % std_out)
        logger.info('std error   : %s' % std_err)
    return None


def windows_path(path, site='SD'):
    if "\\" in path:
        return path
    if '/prj/qct/asw/crmbuilds/' in path:
        if site == "QIPL":
            path = path.replace('/prj/qct/asw/crmbuilds', '')
            return ntpath.normpath(path)
    if '/net' in path:
        path = path.replace('/net', '')
        path = path.replace('/local/mnt/', '')
    elif 'ENG_CRM' in path and site == 'QIPL':
        path = path.replace('/prj/qct/quic/eng_builds1', '/queen/bi_builds')
    elif 'snowcone' in path and site == 'SD':
        path = path.replace('/prj/qct/asw/crmbuilds', '')
    elif 'qcdfs' not in path and site == 'SD':
         for mudpiepath in mudpie_paths:
            if  mudpiepath in path:
                path = path.replace('/prj/qct/quic/', '')
                if mudpiepath == "sd_au_scratch_5":
                   path = '/snowcone/' + path
                   return ntpath.normpath(path)
                path = '/mudpie/' + path
                return ntpath.normpath(path)
         path = '/qcdfs' + path
    elif 'qcthyddfs' not in path and site == 'QIPL':
        for linthyd1 in linthyd_paths:
            if linthyd1 in path:
                path = path.replace('/prj/qct/quic/', '')
                path = '/linthyd1/' + path
                return ntpath.normpath(path)
        for upagrahpath in upagrah_paths:
            if upagrahpath in path:
                path = path.replace('/prj/qct/quic/', '')
                path = '/upagrah/' + path
                return ntpath.normpath(path)
        if 'linthyd1' not in path and 'upagrah' not in path:
            path = '/qcthyddfs/qct' + path
    elif 'nsid-sha' in path and site == 'SH':
        path = '/grilled' + path.replace('/prj/qct/asw/crmbuilds', '')
    elif 'au-binary' in path and site == 'SH':
        path = '/chess/AU-BINARY' + path.replace('/prj/quic/au-binary', '')

    return ntpath.normpath(path)


def format_meta_path(path, site='SD'):
    if not path == ' ':
        m = re.search('/prj/qct/asw/crmbuilds/', path)
        if m:
            meta_path = re.sub('/prj/qct/asw/crmbuilds/', '\\\\', path)
            if site == 'SH' and 'nsid-sha' in path:
                meta_path_win = "\\\\grilled" + ntpath.normpath(meta_path)
            else:
                meta_path_win = "\\" + ntpath.normpath(meta_path)
            logger.info('Meta-build path : %s' % meta_path_win)
        else:
            meta_path_win = windows_path(path, site)
            logger.info('Meta-build path : %s' % meta_path_win)
    return meta_path_win

def extract_build_variant(metabuild_path):
    # e.g. /prj/qct/asw/crmbuilds/snowcone/builds774/INTEGRATION/MSM8996.LA.1.0.1-01500-STD.INT-1
    try:
        return metabuild_path.split('/')[-1].split('-')[-2]
    except IndexError:
        return None

def convert_software_product(software_product):
    # Simple lookup table... if we need to we'll nest additional key/values later.
    SP_Lookup_Tbl = {"MSM8953.LA.0.9": {"SP": "MSM8953.LA.1.0", "Variant": "64bit"}}

    if software_product in SP_Lookup_Tbl:
        return SP_Lookup_Tbl[software_product]
    return { "SP": software_product, "Variant": "Release" }


def external_call(command, capture_output=True):
    print("\tRunning %s" % command)
    errors = None
    output = None
    try:
        if capture_output:
            p = sub.Popen(command, stdout=sub.PIPE, stderr=sub.PIPE,
                          shell=True, universal_newlines=True)
            output, errors = p.communicate()
        else:
            output = os.system(command)
    except Exception as x:
        print("Error executing command '%s'. Reason: %s" % (str(command), x))
        sys.exit(1)
    finally:
        if (errors is not None) and (not errors == ""):
            print("Process stderr: %s" % errors)
    return output


def meta_checker(args, func="get_partition_files"):
    if not args.enable_metacheck:
        return 'pass'

    meta_path = args.metabuildpath
    logger.info('Got Linux meta path: %s' % meta_path)
    cmd = os.path.join(meta_path + "/common/build/app/", "meta_cli.py")
    logger.info('search cmd: %s' % cmd)
    total_tries = 0
    while total_tries < 3:
        if os.path.exists(cmd):
            contentpath = meta_path + "/" + "contents.xml"
            extra_parm = "group=True flavor=\"asic\""
            ret = external_call('python %s %s %s --contentsxml=\"%s\"'
                                % (cmd, func, extra_parm, contentpath))
            if ret is None:
                logger.info("Got nothing after ran it !!")
                break

            logger.info("meta details: %s" % ret)
            ret_d = ast.literal_eval(ret)
            if len(ret_d["partition_patch"]) > 0 and \
               len(ret_d["partition_bin"]) > 0 and \
               len(ret_d["partition"]) > 0:
                return 0
            else:
                logger.info("partition:%d partition_bin:%d partitionpatch:%d" %
                            (len(ret_d["partition"]),
                             len(ret_d["partition_bin"]),
                             len(ret_d["partition_patch"])))
                logger.info("Please offer correct META path!!!")
                break
        else:
            total_tries += 1
            logger.info("The path isn't exist,retry {} times"
                        .format(total_tries))
            time.sleep(15)
    return 1


def launch_test_request(args):

    meta_path = format_meta_path(args.metabuildpath, args.site)
    meta_variant = extract_build_variant(args.metabuildpath)
    SP_Attributes = convert_software_product(args.softwareProduct)

    if SP_Attributes['SP'] == args.softwareProduct:
        logger.info('Software Product: %s' % args.softwareProduct)
    else:
        logger.info('Specified (fake) Software Product: %s' % args.softwareProduct)
        logger.info('Converted (actual) Software Product: %s' % SP_Attributes['SP'])

    logger.info('Meta-build path : %s' % meta_path)
    logger.info('Meta-build variant: %s' % meta_variant)
    if args.ciContacts == 'lnxbuild':
        args.ciContacts = args.submitter

    # ── Public API /events payload format ─────────────────────────────────
    payload = {
            "metaBuild": {
                "path": meta_path,
                "storageType": "UFS",
                "storageLayout": "Auto",
                "productFlavor": "Auto",
                "binaryType": "Auto",
            },
            "branch":          args.branch if args.branch else "",
            "ciSystem":        args.ciFramework,
            "softwareProduct": str(SP_Attributes['SP']),
            "variant":         args.buildVariant if args.buildVariant else "",
            "submitter":       args.submitter,
            "isDagEvent":      True,
        }
    # isgating
    if args.isgating:
        payload['isGating'] = "true"

    # buildvariant
    if args.buildVariant is not None and len(args.buildVariant):
        payload['Variant'] = str(args.buildVariant)

    # purpose
    if args.purpose is not None and len(args.purpose):
        payload['purpose'] = str(args.purpose)

    # Branch
    if args.branch is not None and len(args.branch):
        payload['branch'] = args.branch

    # component
    if args.branchcomponent is not None and len(args.branchcomponent):
        payload['component'] = args.branchcomponent

    # CI Descriptor
    if args.ciDescriptor is not None and len(args.ciDescriptor):
        payload['ciDescriptor'] = args.ciDescriptor

    # Let's construct the JSON payload based on the arguments provided
    if args.buildpath is not None and args.buildpath is not "":
        logger.info('Build path      : %s' % args.buildpath)
        build_path = "\\" + windows_path(args.buildpath, args.site)
        logger.info('Build path      : %s' % build_path)
        payload['images'] = [
            {
                "ImageName": str(args.buildMapping),
                "ImageBuild": {
                    "windows": str(build_path),
                    "linux": "/prj/snowcone"
                    }
                }
            ]
    if args.secondarybuildpath is not None and args.secondarybuildpath is not "":
        logger.info('Build path      : %s' % args.buildpath)
        build_path = "\\" + windows_path(args.buildpath, args.site)
        logger.info('Build path      : %s' % build_path)
        payload['images'].append(
            {
                "ImageName": str(args.secondarybuildMapping),
                "ImageBuild": {
                    "windows": str(build_path),
                    "linux": "/prj/snowcone"
                    }
                })

    # Add scopes item for multi-component
    if args.componentscopes == "":
        logger.info('Not adding scopes to payload as its a non tech pack SI')
    else:
        if args.componentscopes is not None and args.branchcomponent is "":
            payload['scopes'] = args.componentscopes.split(",")
            logger.info('Adding scopes to payload  : %s' % args.componentscopes)

    # Let's add the ciJobURL if specified
    if args.ciJobURL is not None:
        payload['ciUrl'] = args.ciJobURL

    # pass CI job id as SourceId for axiom use case for unqique parameter
    if args.ciJobId is not None:
        payload['SourceId'] = args.ciJobId

    if args.ciJobTAG is not None:
        payload['Tags'] = args.ciJobTAG.split(",")

    # Let's add a chipset to the payload only if it has been provided
    if args.chipset is not None:
        payload['build']['chipset'] = args.chipset
    else:
        if re.search('MSM8939', args.softwareProduct):   # TEMPORARY HACK
            payload['build']['chipset'] = 'MSM8939'

    if args.parentaxiomJson is not None:
        dynamic_playlists = []
        group_id = None

        with open(args.parentaxiomJson, 'r') as axiomjson:
            parent_json = json.loads(axiomjson.read())
            jobs = parent_json.get('Jobs')
            with warnings.catch_warnings():
                for job_id, data in list(jobs.items()):
                    job_url = axiom_BASE_API + JOB_API.format(job_id)
                    job_data = requests.get(job_url, verify=False)
                    if job_data.status_code == 200:
                        job = job_data.json()
                        group_id = job['playlistgroup'].get('id')
                        if group_id:
                            result_url = axiom_BASE_API + RESULTS_API.format(job_id)
                            result_data = requests.get(result_url, verify=False)
                            if result_data.status_code == 200:
                                results = result_data.json()
                                failed_results = []
                                for result in results:
                                    if result.get('result') == 1: # failed
                                        if result.get('case') not in failed_results:
                                            failed_results.append(result.get('case'))
                                logger.info('failed tests ({}): {}'.format(len(failed_results), failed_results))
                                if len(failed_results) == 0:
                                    continue

                                playlist_cases_api = axiom_BASE_API + PLAYLIST_CASES_API.format(group_id)
                                playlist_cases_data = requests.get(playlist_cases_api, verify=False)
                                if playlist_cases_data.status_code == 200:
                                    playlist_cases = playlist_cases_data.json()
                                    for playlist_data in playlist_cases.get('playlists'):
                                        device_properties = []
                                        for playlist in job['playlistgroup'].get('playlists'):
                                            if playlist.get('id') == playlist_data['playlist'].get('id'):
                                                for dp in playlist.get('device_properties'):
                                                    if dp not in device_properties:
                                                        device_properties.append(dp)

                                        playlist = {
                                            'playlist': playlist_data['playlist'].get('id'),
                                            'repeat': playlist_data.get('repeat'),
                                            'timeout': playlist_data['playlist'].get('timeout'),
                                            'load_timeout': playlist_data['playlist'].get('load_timeout'),
                                            'load_attempts': playlist_data['playlist'].get('load_attempts'),
                                            'deviceProperties': device_properties
                                        }

                                        tests = []
                                        for cases_data in playlist_data['playlist'].get('playlistcases'):
                                            if cases_data['case']['id'] in failed_results:
                                                logger.info('adding test: {}'.format(cases_data['case'].get('name')))
                                                tests.append({
                                                    'id': cases_data.get('id'),
                                                    'sequence': cases_data.get('step'),
                                                    'attempts': cases_data.get('attempts'),
                                                    'timeout': cases_data.get('timeout')
                                                })

                                        if len(tests) == 0:
                                            continue

                                        playlist['tests'] = tests
                                        dynamic_playlists.append(playlist)

        if len(dynamic_playlists) > 0:
            payload['CI']['playlistGroup'] = group_id
            payload['CI']['dynamic_playlists'] = dynamic_playlists

    #url = args.axiomurl + '/api/rest/exemgr/start_test/'
    url = args.axiomurl

    logger.info('Sending data to axiom at %s' % url)
    logger.info('\n%s' % json.dumps(payload, sort_keys=True, indent=4, separators=(',', ': ')))

    if args.justPrint:
        sys.exit(0)
    # LINT seems to have an older version of requests, so need to manually
    # set the html header for JSON and pass the JSON data as a string.
    # For newer versions can omit headers and do
    # requests.post(url, json=payload)
    global _OAUTH_TOKEN
    if _OAUTH_TOKEN is None:
        _OAUTH_TOKEN = _get_oauth_token()
    if _OAUTH_TOKEN:
        headers = _public_headers()
        logger.info('Using Apigee public API with OAuth Bearer token')
    else:
        headers = {'Content-Type': 'application/json'}
        logger.warning('No OAuth token — falling back to unauthenticated request')

    total_tries = 0
    ci_job_id_str = args.ciJobId if args.ciJobId is not None else "0"
    event_url = args.axiomurl + "/source?sourceId=" + ci_job_id_str + "&product=" + args.softwareProduct
    logger.info(event_url)
    while total_tries < args.retrycnt:
        try:
            logger.info('Sending request to Axiom')
            response = requests.post(url, json.dumps(payload), headers=headers, verify=False)
            logger.info('Recieved response from Axiom')
            logger.info('Response preload: %s' % json.dumps(payload))
            logger.info('Response code: %i' % response.status_code)
            logger.info('Response text: %s' % response.text)

            # If successful, display the job_status_url
            if response.status_code == 200:
                results = json.loads(response.text)
                # Public API returns a list [{jobId, team, jobMode, jobType, createdOn}]
                # Normalise to the shape the rest of the script expects:
                # {url, status, jobs:[{id, state, ...}]}
                if isinstance(results, list):
                    job_ids = [r['jobId'] for r in results if 'jobId' in r]
                    logger.info('Public API submitted jobIds: %s', job_ids)
                    normalised = {
                        'url': '/#/reports/job/' + str(job_ids[0]) if job_ids else '',
                        'status': 'Submitted',
                        'jobs': [{'id': jid, 'state': 'Submitted', 'name': None,
                                  'apiUrl': None, 'jobVerdict': None,
                                  'passCount': 0, 'failCount': 0,
                                  'isGating': False, 'other': 0} for jid in job_ids],
                        'jobIds': job_ids,
                    }
                    results = normalised
                return results


            # If reached here, some other error code, wait and retry
            raise Exception("Unable to handle HTTP response")

        except Exception as e:
            logger.error(str(e))
            total_tries += 1
            time.sleep(args.pollingInterval)
            for retry in range(args.retrycnt):
                try:
                    event_data = requests.get(event_url, verify=False)
                    if event_data.status_code == 200:
                        results = event_data.json()
                        if results is not None and results['events'] is not None:
                            logger.info('Getting already existing job')
                            Update_EC_Properties("/myCall/axiom_report_url",
                                                 results['events'][0],
                                                 args.supressEC)
                            logger.info(results['events'][0])
                            sys.exit(0)
                except Exception as e:
                    logger.error(str(e))
                    logger.info('Axiom event api return nothing retrying')


def is_any_job_running(joblist):
    for job in list(joblist.values()):
        if not axiom_wholejob_is_done(job):
            return True
    return False


def axiom_wholejob_is_done(job):
    if job['status'] in ("Completed Successfully",
                         "Completed Unsuccessfully",
                         'Aborted', 'Timeout'):
        return True
    return False


def axiom_job_is_done_by_fail(job):
    if job['status'] in ("Completed Unsuccessfully", 'Aborted', 'Timeout'):
        return True
    return False


def axiom_subjob_is_done_by_fail(job):
    if job['state'] in ('Completed Unsuccessfully', 'Aborted', 'Timeout'):
        return True
    return False


def send_email_alert_to_apt_team(job, alert=False):
    axiom_event_url = PUBLIC_API_BASE + "/events/" + str(args.monitorid)
    axiom_job_url = "https://axiom.qualcomm.com/#/reports/job/{}" \
                    .format(str(job))
    ec_jobid = str(args.ciJobURL)
    if alert:
        subject = 'Axiom {0} Job :{1}_{2} Running more than 2:30 hours' \
                  .format(args.branch, args.buildMapping, args.softwareProduct)
    else:
        subject = 'Axiom {0} Job failed :{1}_{2} with Infra issues' \
                  .format(args.branch, args.buildMapping, args.softwareProduct)
    mail_text = []
    email_list = ['APT.PF.core@qti.qualcomm.com',
                  'lint.pf@qti.qualcomm.com',
                  'iot.apt.PF@qti.qualcomm.com']
    mail_text.append("Axiom Event Id: %s\n\n"
                     "Axiom Job Id: %s\n\n"
                     "LINT EC Job Link: %s\n\n" % (axiom_event_url,
                                                   axiom_job_url,
                                                   ec_jobid))
    if alert:
        mail_text.append("Preflight RTI job is running more than 2:30 hours "
                         " Please check and add devices are disable unwanted "
                         " test cases, in another 30 minutes Preflight job "
                         " will fail with timeout \n\n")
    else:
        mail_text.append("Please restart the JobId immediately if it's failed "
                         "with Infra issues, we have 20 minutes wait time to "
                         " reverify the failed Job\n\n")
    mail_text.append("Thanks,\nLINT TEAM\n")
    try:
        Email(email_list, subject, '\n'.join(mail_text)).send()
    except Exception as e:
        logger.info('Email Sending notification failed')
        logger.error(str(e))


def calculateOverallResult(jobs):

    overall = 'SUCCESS'
    logger.info('Calculating Overall result based on jobs:{}'
                .format(str(jobs)))

    for jobid, jobdetails in list(jobs.items()):
        if jobdetails['status'] in ("Completed Unsuccessfully",
                                    "Running", "Submitted",
                                    "Queued", "Job Setup"):
            jobdetails['status'] = "Completed Unsuccessfully"
            overall = '{0}:{1}'\
                      .format(str(jobdetails['status']),
                              str(jobdetails['jobVerdict']))
            Update_EC_Properties("/myCall/axiom_outcome",
                                 overall, args.supressEC)
            return

    for jobid, jobdetails in list(jobs.items()):
        if axiom_job_is_done_by_fail(jobdetails):
            overall = '{0}:{1}'\
                      .format(str(jobdetails['status']),
                              str(jobdetails['jobVerdict']))
            break

    # Now save the calculated result to LINT
    Update_EC_Properties("/myCall/axiom_outcome", overall, args.supressEC)


def get_job_status_from_server(monitorurl, args):
    total_tries = 0
    while total_tries < args.retrycnt:
        try:
            _hdrs = _public_headers() if _OAUTH_TOKEN else {}
            response = requests.get(monitorurl, headers=_hdrs, verify=False)

            if response.status_code == 200:
                jobresults = json.loads(response.text)
                logger.info('Response text:{}'.format(jobresults))
                return jobresults

            raise Exception("Unable to handle HTTP response")

        except Exception as e:
            logger.error(str(e))
            total_tries += 1
            time.sleep(args.pollingInterval)


def monitor_test_execution(args):

    trigger_aborts = False
    axiom_jobs = []
    axiom_issues = [('Completed Unsuccessfully', None),
                    ('Completed Unsuccessfully', 'InfraIssue'),
                    ('Completed Unsuccessfully', 'ProductSoftwareIssue'),
                    ('Aborted', 'SetupIssue'),
                    ('Aborted', 'ProductSoftwareIssue'),
                    ('Aborted', None)]

    if args.blockingmode == 0:
        logger.info('')
        logger.info('This Target is configured in non-blocking mode.')
        logger.info('')
        sys.exit(0)

    axiom_event_url = "https://api.axiom.qualcomm.com/event/{}" \
                      .format(str(args.monitorid))

    logger.info('url: {}'.format(axiom_event_url))
    results = get_job_status_from_server(axiom_event_url, args)
    if results is None:
        logger.info('Something wrong is from server!!')
        exit(0)

    logger.info('Result: {}'.format(results))
    logger.info('')
    logger.info('axiom Job ID    Playlist Group Name               Status')
    logger.info('------------    ----------------------    --------------')
    axiom_event_status = results['status']
    for result in results['jobs']:
        monitor_jobs[result['id']] = {'status': result['state'],
                                      'apiurl': result['apiUrl'],
                                      'name': result['name'],
                                      'id': result['id'],
                                      'jobVerdict': result['jobVerdict'],
                                      'passcount': result['passCount'],
                                      'failcount': result['failCount'],
                                      'isgating': result['isGating'],
                                      'other': result['other']}
        logger.info('%12s    %-30s    %-15s' %
                    (result['id'], result['name'], result['state']))

    logger.info('')
    logger.info('Total axiom Job(s) started: %d' % len(monitor_jobs))
    logger.info('')

    while is_any_job_running(monitor_jobs):

        jobresults = get_job_status_from_server(axiom_event_url, args)

        # update job parameters
        for job in jobresults['jobs']:
            jobid = job['id']
            current_time = time.time()
            if current_time > args.alert_time and args.branch == "Preflight":
                logger.info('Sending alert mail to APT team *****')
                send_email_alert_to_apt_team(jobid, True)
                args.alert_time = args.alert_time + 1800
            monitor_jobs[jobid] = {'status': job['state'],
                                   'apiurl': job['apiUrl'],
                                   'name': job['name'],
                                   'id': job['id'],
                                   'passcount': job['passCount'],
                                   'failcount': job['failCount'],
                                   'jobVerdict': job['jobVerdict'],
                                   'isgating': job['isGating'],
                                   'other': job['other']}

            if axiom_subjob_is_done_by_fail(job):
                logger.info('Axiom {0} failed,status:{1} Verdict:{2}'
                            .format(str(job['apiUrl']), str(job['state']),
                                    str(job['jobVerdict'])))
                if args.branch == "Preflight":
                    for issue in axiom_issues:
                        if (job['state'] in issue and
                                job['jobVerdict'] == issue[1]):
                            if jobid not in axiom_jobs:
                                axiom_jobs.append(jobid)
                                logger.info('Sending failed job email alert')
                                send_email_alert_to_apt_team(jobid)

        logger.info('Axiom eventjob status {}'
                    .format(str(jobresults['status'])))

        # To do forcing abort job
        trigger_aborts = False
        # LETS CHECK TO SEE IF ANYTHING HAS FAILED AND
        # IF WE NEED TO ABORT ANY PENDING JOBS
        if trigger_aborts and args.abort_jobs_on_failure:
            for jobid, job in list(monitor_jobs.items()):
                abort_job(args, jobid)
                monitor_jobs[jobid]['status'] = 'FORCE_ABORTED'
        time.sleep(args.refreshrate)
    # No more pending jobs.  Set the final result and return
    for jobid, jobdetails in list(monitor_jobs.items()):
        job_status = jobdetails['status']
        job_verdict = jobdetails['jobVerdict']
        if job_status == 'Completed Unsuccessfully':
            break
        if job_status == 'Aborted' and job_verdict is None:
            break
    if job_status != "Completed Successfully":
        timeout = 1200
        timeout_start = time.time()
        logger.info('before monitor jobs result {}'.format(str(monitor_jobs)))
        while (time.time() < timeout_start + timeout and
               args.branch == "Preflight"):
            jobresults = get_job_status_from_server(axiom_event_url, args)
            logger.info('Waiting Response text:{}'.format(jobresults))
            event_status = jobresults['status']
            if event_status == 'Queued' or event_status == 'Running':
                timeout = timeout + 60
            if event_status == 'Completed':
                job_status = True
            else:
                job_status = False
            for job in jobresults['jobs']:
                jobid = job['id']
                current_time = time.time()
                if (current_time > args.alert_time and
                        args.branch == "Preflight"):
                    logger.info('Sending alert mail to APT team *****')
                    send_email_alert_to_apt_team(jobid, True)
                    args.alert_time = args.alert_time + 1800
                monitor_jobs[jobid]['status'] = job['state']
                monitor_jobs[jobid]['jobVerdict'] = job['jobVerdict']
                for issue in axiom_issues:
                    if job['state'] in issue and job['jobVerdict'] == issue[1]:
                        logger.info('Axiom infra issues waiting for 20 \
                            minutes until the status Completed Successfully')
                        if jobid not in axiom_jobs:
                            axiom_jobs.append(jobid)
                            send_email_alert_to_apt_team(jobid)
                        job_status = False
            if job_status:
                break
            time.sleep(args.refreshrate)
    logger.info('after monitor jobs result {}'.format(str(monitor_jobs)))
    calculateOverallResult(monitor_jobs)


def abort_job(args, job_id):
    abort_job_url = '%s/api/rest/exemgr/jobs/%d/abort/' % (args.axiomurl, job_id)
    logger.info('Submitting job abort request to axiom using %s' % abort_job_url)
    try:
        response = requests.post(abort_job_url, verify=False)
        logger.info('Abort Response code: %i' % response.status_code)
        logger.info('Response text: %s' % response.text)
    except Exception as e:
        logger.error(str(e))

    return None


def signal_handler(signal, frame):
    """
    Capture Ctrl+C by user or other process
    """
    logger.error("   Ctrl+C detected, aborting all running/pending jobs...")

    for jobid, job in list(monitor_jobs.items()):
        abort_job(args, jobid)

    # On ctl+C, should we update the outcome to ABORTED?
    Update_EC_Properties("/myCall/outcome", 'ABORTED', args.supressEC)
    sys.exit(2)

if __name__ == '__main__':
    FORMAT = '[%(asctime)s : - %(levelname)s - %(message)s'
    logging.basicConfig(format=FORMAT)
    logger.setLevel(logging.INFO)

    args = process_arguments()
    args.alert_time = time.time() + 9000

    if args.monitorid is not None:
        monitor_test_execution(args)
        exit(0)

    # Catch interrupts,
    # signal.signal(signal.SIGINT, signal_handler)
    # signal.signal(signal.SIGTERM, signal_handler)
    # signal.signal(signal.SIGBREAK, signal_handler)
    if args.enable_metacheck:
        exit(meta_checker(args))

    results = launch_test_request(args)

    if results is not None \
       and re.search("NoAction", results['status']) is None:
        logger.info('The Axiom job has been triggered correctly!')
        Update_EC_Properties("/myCall/axiom_report_url",
                             results['url'], args.supressEC)
        sys.exit(0)
    else:
        logger.error('Something went totally wrong and we need to exit.')
        Update_EC_Properties("/myCall/axiom_retry_failed",
                             "retry-failed", args.supressEC)
        sys.exit(0)
