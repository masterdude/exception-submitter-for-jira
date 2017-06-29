import base64
import io
import logging
import re
import requests
from codecs import *
from copy import deepcopy
from datetime import datetime
from difflib import SequenceMatcher
from flask import request, jsonify, json
from operator import itemgetter
from urllib.parse import *

from exceptionservice import app
from exceptionservice.config import *

MAX_SUMMARY_LENGTH = 255
MAX_DESCRIPTION_LENGTH = 32767 - 767  # Max chars but trim only the stacktrace so leave enough room for other text
BLACKLISTED_CHARACTERS = "'\"+-,?|*/%^$#@[]()&"  # as per JQL spec + some reverse engineering

"""
This is the base-class with views
"""

__author__ = 'Miel Donkers <miel.donkers@codecentric.nl>'
__credits = ['Serkan Demirel <serkan@blackbuilt.nl>']

log = logging.getLogger(__name__)

_JIRA_URI_SEARCH = urljoin(JIRA_URI, '/rest/api/latest/search')
_JIRA_URI_CREATE_UPDATE = urljoin(JIRA_URI, '/rest/api/latest/issue')
_JIRA_URI_CURRENT_SPRINT = urljoin(JIRA_URI, 'rest/agile/1.0/board/{}/sprint?state=active'.format(JIRA_BOARD_ID))
_JIRA_USER_PASSWD = (JIRA_USER, JIRA_PASSWD)
_JIRA_FIELDS = ['id', 'key', 'created', 'status', 'labels', 'summary', 'description', 'environment', 'fixVersions']
_CONTENT_JSON_HEADER = {'Content-Type': 'application/json'}
_JIRA_TRANSITION_REOPEN_ID = '3'

REGEX_CAUSED_BY = re.compile(r'\W*caused\W+by', re.IGNORECASE)
REGEX_COUNT = re.compile(r'.*count:\s+(\d+)', re.IGNORECASE)


class InternalError(Exception):
    """Exception raised for all internal errors.

    Attributes:
        message -- explanation of the error
    """

    def __init__(self, message):
        self.message = message


@app.route('/', methods=['GET', 'POST'])
def receive_exception():
    try:
        if request.method == 'POST' and request.is_json:
            return add_jira_exception(request.get_json())
        else:
            return jsonify(show_all_open_issues())
    except InternalError as err:
        log.error('Error during processing:', exc_info=err)
        return 'Error during processing; \n\t{}'.format(err), 500, {}


def show_all_open_issues():
    query = {'jql': 'project={}&status in (Open,"In Progress",Reopened)&issuetype=Bevinding'.format(JIRA_PROJECT),
             'fields': _JIRA_FIELDS}
    resp = requests.post(_JIRA_URI_SEARCH,
                         json=query,
                         headers=_CONTENT_JSON_HEADER,
                         auth=_JIRA_USER_PASSWD)

    if resp.status_code != 200:
        raise InternalError('Could not get open Jira issues. HTTP response code {} : {}'.format(resp.status_code, resp.content))

    return resp.json()


def get_current_sprint():
    resp = requests.get(_JIRA_URI_CURRENT_SPRINT,
                        headers=_CONTENT_JSON_HEADER,
                        auth=_JIRA_USER_PASSWD)
    current_sprint = resp.json()['values'][0]['name']  # there is only 1 active sprint
    log.info("Current sprint {}".format(current_sprint))
    return current_sprint


def add_jira_exception(json_data):
    log_received_json_without_binary(json_data)

    is_duplicate = determine_if_duplicate(json_data)

    if is_duplicate[0]:
        issue_id = is_duplicate[1]
        fixed_in_sprint = is_duplicate[4]
        should_reopen = should_reopen_if_closed(is_issue_closed(is_duplicate[2]), fixed_in_sprint)
        update_to_jira(issue_id, calculate_issue_occurrence_count(is_duplicate[3]), should_reopen)
        update_issue_with_attachments(json_data, issue_id)
        return 'Jira issue already exists, updated: {}'.format(issue_id)

    result = add_to_jira(get_summary_from_message(json_data), create_details_string_from_json(json_data), get_stacktrace_from_message(json_data))
    issue_id = result['key']
    update_issue_with_attachments(json_data, issue_id)
    return 'Jira issue added: {}'.format(issue_id), 201, {}


def should_reopen_if_closed(issue_is_closed, fixed_in_sprint):
    current_sprint = get_current_sprint()
    is_closed_in_current_sprint = current_sprint == fixed_in_sprint
    return issue_is_closed and not is_closed_in_current_sprint


def log_received_json_without_binary(json_data):
    dict_without_binary = deepcopy(json_data)

    if 'screenshots' in dict_without_binary:
        del dict_without_binary['screenshots']

    if 'logs' in dict_without_binary:
        del dict_without_binary['logs']

    log.info('\n\n----------\nReceived json data: {}'.format(json.dumps(dict_without_binary)))


def update_issue_with_attachments(json_data, issue_id):
    add_attachment(get_stacktrace_from_message(json_data), 'text', 'stacktrace.txt', issue_id)

    if 'logs' in json_data:
        add_attachment(base64.b64decode(json_data['logs']), 'binary', 'logfiles.zip', issue_id)

    if 'screenshots' in json_data:
        for b64_encoded_screenshot in json_data['screenshots']:
            add_attachment(base64.b64decode(b64_encoded_screenshot), 'binary', 'screenshot.jpg', issue_id)


def is_issue_closed(status):
    return status.lower() == 'closed' or status.lower() == 'resolved'


def calculate_issue_occurrence_count(existing_count):
    count = 1

    if existing_count is not None and len(existing_count) > 0:
        match = REGEX_COUNT.match(existing_count)
        if match:
            count = int(match.group(1)) + 1

    return 'Count: {}\nLast: {}'.format(count, datetime.now())


def determine_if_duplicate(json_data):
    exception_summary = get_summary_from_message(json_data)
    issue_list = find_existing_jira_issues(exception_summary)

    new_stacktrace = get_stacktrace_from_message(json_data)
    for issue in issue_list:
        issue_stacktrace = get_stacktrace_from_issue(issue)
        new_trimmed_stacktrace = new_stacktrace[:len(issue_stacktrace)]  # Trim to same length as Jira issue might have been trimmed
        s = SequenceMatcher(lambda x: x == ' ' or x == '\n' or x == '\t',
                            new_trimmed_stacktrace,
                            issue_stacktrace)

        match_ratio = s.ratio() if s.real_quick_ratio() > 0.6 else 0
        if len(issue_stacktrace) > 0 and match_ratio > 0.7 and matches_exception_throw_location(new_trimmed_stacktrace, issue_stacktrace):
            log.info('\nMatch ratio: {} for stacktrace:\n{}'.format(match_ratio, issue_stacktrace))
            return True, \
                   issue['key'], issue['fields']['status']['name'], \
                   issue['fields']['environment'], \
                   get_latest_fix_version(issue['fields']['fixVersions'])['name']
    return False, ''


def get_latest_fix_version(fix_versions):
    return sorted(fix_versions, key=itemgetter('name'), reverse=True)[0]


def sanitize_jql_summary(raw, trim_for_query=False):
    # certain characters are not allowed by JQL
    sanitized = filter_out_blacklisted_characters(raw)

    # trim unnecessary whitespaces; '        ' to ' '
    sanitized = trim_whitespace(sanitized)

    # cap the summary field to the allowed maximum
    max_length = MAX_SUMMARY_LENGTH - len(JIRA_ISSUE_TITLE) - 2
    sanitized = trim_length(sanitized, max_length)

    if trim_for_query and sanitized.count(':') >= 2:
        # For querying, trim after the second colon, otherwise it might contain too much rubbish
        sanitized = sanitized[:sanitized.find(':', sanitized.find(':') + 1)]

    return sanitized


def trim_length(input, max_length):
    return input[:max_length] if len(input) > max_length else input


def filter_out_blacklisted_characters(input):
    sanitized = ""
    for c in input:
        if c not in BLACKLISTED_CHARACTERS:
            sanitized += c
    return sanitized


def trim_whitespace(input):
    return re.sub('\s+', ' ', input).strip()


def find_existing_jira_issues(exception_summary, start_at=0):
    query = {'jql': "project={}&issuetype=Bevinding&summary ~ '{}'".format(JIRA_PROJECT, sanitize_jql_summary(exception_summary, True)),
             'startAt': str(start_at),
             'fields': _JIRA_FIELDS}
    resp = requests.post(_JIRA_URI_SEARCH,
                         json=query,
                         headers=_CONTENT_JSON_HEADER,
                         auth=_JIRA_USER_PASSWD)
    if resp.status_code != 200:
        raise InternalError('Could not query Jira issues, cancel processing issue. HTTP response code {} : {}'.format(resp.status_code, resp.content))

    max_results = resp.json()['maxResults']
    total_results = resp.json()['total']
    issue_list = find_existing_jira_issues(exception_summary, start_at + max_results) if total_results > start_at + max_results else list()

    return issue_list + resp.json()['issues']


def get_stacktrace_from_issue(issue):
    description = issue['fields']['description']
    description_blocks = description.split('{noformat}')
    if len(description_blocks) >= 3:
        return description_blocks[1]
    else:
        return ''


def matches_exception_throw_location(new_stacktrace, issue_stacktrace):
    line_new_stacktrace = first_line_caused_by_from_printed_stacktrace(new_stacktrace)
    line_issue_stacktrace = first_line_caused_by_from_printed_stacktrace(issue_stacktrace)

    return line_new_stacktrace == line_issue_stacktrace


def first_line_caused_by_from_printed_stacktrace(printed_stacktrace):
    lines = printed_stacktrace.splitlines()
    loc_last_causedby_line = -1
    for i in range(len(lines)):
        if REGEX_CAUSED_BY.match(lines[i]):
            loc_last_causedby_line = i

    # Split at the colon, first element of tuple contains entire string if colon not found
    exception_line = lines[loc_last_causedby_line + 1].partition(':')
    return exception_line[0]


def create_details_string_from_json(json_data):
    dict_without_attachments = deepcopy(json_data)

    if 'stacktrace' in dict_without_attachments:
        del dict_without_attachments['stacktrace']

    if 'screenshots' in dict_without_attachments:
        del dict_without_attachments['screenshots']

    if 'logs' in dict_without_attachments:
        del dict_without_attachments['logs']

    output = ''
    for key, value in dict_without_attachments.items():
        output += '  {}: {}\n'.format(key, value)

    return output


def get_summary_from_message(json_data):
    # Get the original exception, which is the last in the list
    stacks = json_data['stacktrace']
    return stacks[len(stacks) - 1]['message']


def get_stacktrace_from_message(json_data):
    traces = json_data['stacktrace']
    output = io.StringIO()
    for trace in traces:
        output.write('Caused by: {}\n'.format(trace['message']))
        for line in trace['stacktrace']:
            if not line['nativeMethod']:  # Filter out native Java methods
                output.write('\tat {}.{}({}:{})\n'.format(line['className'], line['methodName'], line['fileName'], line['lineNumber']))

    result = output.getvalue()
    output.close()
    return result


def add_to_jira(summary, details, stacktrace):
    summary = sanitize_jql_summary(summary)
    title = '{}: {}'.format(JIRA_ISSUE_TITLE, summary)
    description = '{}\n\nDetails:\n{}\n\nStacktrace:\n{{noformat}}{}{{noformat}}'.format(summary, details, trim_length(stacktrace, MAX_DESCRIPTION_LENGTH))
    issue = {'project': {'key': '{}'.format(JIRA_PROJECT)}, 'summary': title, 'description': description,
             'issuetype': {'name': 'Bevinding'}, 'labels': ['Beheer']}
    fields = {'fields': issue}

    log.info('Sending:\n{}'.format(json.dumps(fields)))

    resp = requests.post(_JIRA_URI_CREATE_UPDATE,
                         json=fields,
                         headers=_CONTENT_JSON_HEADER,
                         auth=_JIRA_USER_PASSWD)
    if resp.status_code != 201:
        raise InternalError('Could not create new Jira issue. HTTP response code {} : {}'.format(resp.status_code, resp.content))

    return resp.json()


def update_to_jira(issue_id, environment, do_status_transition):
    updated_fields = {'environment': [{'set': environment}]}
    fields = {'update': updated_fields}

    log.info('Sending:\n{}'.format(json.dumps(fields)))
    resp = requests.put(urljoin(_JIRA_URI_CREATE_UPDATE + '/', issue_id),
                        json=fields,
                        headers=_CONTENT_JSON_HEADER,
                        auth=_JIRA_USER_PASSWD)

    if resp.status_code != 204:
        raise InternalError('Could not update existing Jira issue. HTTP response code {} : {}'.format(resp.status_code, resp.content))

    if do_status_transition:
        log.info('Update issue status to: {}'.format(_JIRA_TRANSITION_REOPEN_ID))
        resp = requests.post(urljoin(_JIRA_URI_CREATE_UPDATE + '/', issue_id + '/transitions'),
                             json={'transition': {'id': _JIRA_TRANSITION_REOPEN_ID}},
                             headers=_CONTENT_JSON_HEADER,
                             auth=_JIRA_USER_PASSWD)
        log.debug('Transition response: ' + resp.text)


def add_attachment(attachment, type, filename, issue_id):
    try:
        # invoke stream method for provided type
        files = {'file': (filename, get_stream_method(type)(attachment))}

        url = urljoin(_JIRA_URI_CREATE_UPDATE + '/', issue_id + '/attachments')
        log.info('Posting attachment to {}'.format(url))

        response = requests.post(url,
                                 headers={'X-Atlassian-Token': 'no-check'},
                                 auth=_JIRA_USER_PASSWD,
                                 files=files)

        log.info('Response for posting attachment to {}: {}'.format(url, response))
    except InternalError as error:
        log.error('Error while adding attachment : {}'.format(error))


def get_stream_method(type):
    return {
        'text': io.StringIO,
        'binary': io.BytesIO,
    }[type]
