import datetime
from dateutil import relativedelta
import requests
import os
from lxml import etree
import time
import hashlib

# Ensure cache directory exists
os.makedirs('cache', exist_ok=True)

HEADERS = {'authorization': 'token ' + os.environ.get('ACCESS_TOKEN', '')}
USER_NAME = os.environ.get('USER_NAME', 'OrixCore')
QUERY_COUNT = {'user_getter': 0, 'follower_getter': 0, 'graph_repos_stars': 0, 'recursive_loc': 0, 'graph_commits': 0, 'loc_query': 0}

# طول كل سطر بالكارد بالحرف (ثابت لكل الأسطر، محسوب من الأسطر الثابتة: OS/Host/Kernel/IDE)
LINE_WIDTH = 96


def daily_readme(birthday):
    """
    Returns the length of time since birth
    """
    diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
    return '{} {}, {} {}, {} {}{}'.format(
        diff.years, 'year' + format_plural(diff.years),
        diff.months, 'month' + format_plural(diff.months),
        diff.days, 'day' + format_plural(diff.days),
        ' 🎂' if (diff.months == 0 and diff.days == 0) else '')


def format_plural(unit):
    return 's' if unit != 1 else ''


def simple_request(func_name, query, variables):
    request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables': variables}, headers=HEADERS)
    if request.status_code == 200:
        return request
    raise Exception(func_name, ' has failed with a', request.status_code, request.text, QUERY_COUNT)


def graph_commits(start_date, end_date):
    query_count('graph_commits')
    query = '''
    query($start_date: DateTime!, $end_date: DateTime!, $login: String!) {
        user(login: $login) {
            contributionsCollection(from: $start_date, to: $end_date) {
                contributionCalendar {
                    totalContributions
                }
            }
        }
    }'''
    variables = {'start_date': start_date, 'end_date': end_date, 'login': USER_NAME}
    request = simple_request(graph_commits.__name__, query, variables)
    return int(request.json()['data']['user']['contributionsCollection']['contributionCalendar']['totalContributions'])


def graph_repos_stars(count_type, owner_affiliation, cursor=None):
    query_count('graph_repos_stars')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            stargazers {
                                totalCount
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(graph_repos_stars.__name__, query, variables)
    if request.status_code == 200:
        if count_type == 'repos':
            return request.json()['data']['user']['repositories']['totalCount']
        elif count_type == 'stars':
            return stars_counter(request.json()['data']['user']['repositories']['edges'])


def recursive_loc(owner, repo_name, data, cache_comment, addition_total=0, deletion_total=0, my_commits=0, cursor=None):
    query_count('recursive_loc')
    query = '''
    query ($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
                            totalCount
                            edges {
                                node {
                                    ... on Commit {
                                        committedDate
                                    }
                                    author {
                                        user {
                                            id
                                        }
                                    }
                                    deletions
                                    additions
                                }
                            }
                            pageInfo {
                                endCursor
                                hasNextPage
                            }
                        }
                    }
                }
            }
        }
    }'''
    variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor}
    request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables': variables}, headers=HEADERS)
    if request.status_code == 200:
        if request.json()['data']['repository']['defaultBranchRef'] is not None:
            return loc_counter_one_repo(owner, repo_name, data, cache_comment, request.json()['data']['repository']['defaultBranchRef']['target']['history'], addition_total, deletion_total, my_commits)
        else:
            return 0, 0, 0
    force_close_file(data, cache_comment)
    if request.status_code == 403:
        raise Exception('Too many requests in a short amount of time!\nYou\'ve hit the non-documented anti-abuse limit!')
    raise Exception('recursive_loc() has failed with a', request.status_code, request.text, QUERY_COUNT)


def loc_counter_one_repo(owner, repo_name, data, cache_comment, history, addition_total, deletion_total, my_commits):
    for node in history['edges']:
        if node['node']['author']['user'] == OWNER_ID:
            my_commits += 1
            addition_total += node['node']['additions']
            deletion_total += node['node']['deletions']

    if not history['edges'] or not history['pageInfo']['hasNextPage']:
        return addition_total, deletion_total, my_commits
    else:
        return recursive_loc(owner, repo_name, data, cache_comment, addition_total, deletion_total, my_commits, history['pageInfo']['endCursor'])


def loc_query(owner_affiliation, comment_size=0, force_cache=False, cursor=None, edges=[]):
    query_count('loc_query')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            defaultBranchRef {
                                target {
                                    ... on Commit {
                                        history {
                                            totalCount
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(loc_query.__name__, query, variables)
    if request.json()['data']['user']['repositories']['pageInfo']['hasNextPage']:
        edges += request.json()['data']['user']['repositories']['edges']
        return loc_query(owner_affiliation, comment_size, force_cache, request.json()['data']['user']['repositories']['pageInfo']['endCursor'], edges)
    else:
        return cache_builder(edges + request.json()['data']['user']['repositories']['edges'], comment_size, force_cache)


def cache_builder(edges, comment_size, force_cache, loc_add=0, loc_del=0):
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    try:
        with open(filename, 'r') as f:
            data = f.readlines()
    except FileNotFoundError:
        data = []
        if comment_size > 0:
            for _ in range(comment_size):
                data.append('This line is a comment block. Write whatever you want here.\n')
        with open(filename, 'w') as f:
            f.writelines(data)

    if len(data) - comment_size != len(edges) or force_cache:
        flush_cache(edges, filename, comment_size)
        with open(filename, 'r') as f:
            data = f.readlines()

    cache_comment = data[:comment_size]
    data = data[comment_size:]
    for index in range(len(edges)):
        repo_hash, commit_count, *__ = data[index].split()
        if repo_hash == hashlib.sha256(edges[index]['node']['nameWithOwner'].encode('utf-8')).hexdigest():
            try:
                if int(commit_count) != edges[index]['node']['defaultBranchRef']['target']['history']['totalCount']:
                    owner, repo_name = edges[index]['node']['nameWithOwner'].split('/')
                    loc = recursive_loc(owner, repo_name, data, cache_comment)
                    data[index] = repo_hash + ' ' + str(edges[index]['node']['defaultBranchRef']['target']['history']['totalCount']) + ' ' + str(loc[2]) + ' ' + str(loc[0]) + ' ' + str(loc[1]) + '\n'
            except TypeError:
                data[index] = repo_hash + ' 0 0 0 0\n'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    for line in data:
        loc = line.split()
        loc_add += int(loc[3])
        loc_del += int(loc[4])
    return [loc_add, loc_del, loc_add - loc_del, True]


def flush_cache(edges, filename, comment_size):
    try:
        with open(filename, 'r') as f:
            data = f.readlines()[:comment_size]
    except FileNotFoundError:
        data = []
    with open(filename, 'w') as f:
        f.writelines(data)
        for node in edges:
            f.write(hashlib.sha256(node['node']['nameWithOwner'].encode('utf-8')).hexdigest() + ' 0 0 0 0\n')


def force_close_file(data, cache_comment):
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)


def stars_counter(data):
    total_stars = 0
    for node in data:
        total_stars += node['node']['stargazers']['totalCount']
    return total_stars


# =========================================================================
#  منطق المحاذاة الجديد
# =========================================================================
#
# الفكرة: كل الأسطر بالكارد (الثابتة منها والديناميكية) طولها الكلي بالحرف
# لازم يساوي LINE_WIDTH (=96) بالضبط، لأن الخط أحادي التباعد (monospace).
# بدل ما نخمن عدد نقاط ثابت، منحسبه رياضياً بحيث الناتج يطلع بنفس الطول
# دايماً، بغض النظر عن طول الرقم/النص.
#
# للأسطر يلي فيها قسمين بنفس السطر (Repos|Stars, Commits|Followers) في
# منطق إضافي: نحدد عمود ثابت (MID_COL) لازم يبلش عنده القسم الثاني، وبعدين
# نحسب نقاط كل قسم على حدة بحيث ينتهي بالظبط عند ذاك العمود.

def make_dots(n):
    """
    يرجع نص نقاط (' .... ') طوله بالحرف يساوي n بالضبط.
    """
    n = max(0, n)
    if n == 0:
        return ''
    if n == 1:
        return ' '
    if n == 2:
        return '. '
    if n == 3:
        return ' . '
    return ' ' + ('.' * (n - 2)) + ' '


def fmt_value(value):
    """يحول الأرقام لنص بفواصل الآلاف، ويترك النصوص كما هي."""
    if isinstance(value, int):
        return '{:,}'.format(value)
    return str(value)


def build_single_section(key, value, target_col, prefix='. '):
    """
    يبني نص سطر كامل (مفتاح + نقاط + قيمة) بحيث طوله الكلي = target_col بالضبط.
    يرجع (dots_text, formatted_value) عشان نحطهم بعناصر SVG منفصلة.
    """
    val_s = fmt_value(value)
    fixed_len = len(prefix) + len(key) + 1 + len(val_s)  # prefix + key + ':' + value
    dots_len = max(0, target_col - fixed_len)
    return make_dots(dots_len), val_s


def build_dual_section(key1, val1, middle_text, key2, val2, mid_col, total_col=LINE_WIDTH, prefix='. '):
    """
    يبني سطر فيه قسمين (متل Repos...Stars أو Commits...Followers).
    - القسم الأول: prefix + key1 + ':' + dots1 + val1 + middle_text  -> ينتهي عند mid_col
    - القسم الثاني: key2 + ':' + dots2 + val2                        -> ينتهي عند total_col
    يرجع (dots1, val1_s, dots2, val2_s)
    """
    val1_s = fmt_value(val1)
    val2_s = fmt_value(val2)

    fixed1_len = len(prefix) + len(key1) + 1 + len(val1_s) + len(middle_text)
    dots1_len = max(0, mid_col - fixed1_len)

    fixed2_len = len(key2) + 1 + len(val2_s)
    dots2_len = max(0, (total_col - mid_col) - fixed2_len)

    return make_dots(dots1_len), val1_s, make_dots(dots2_len), val2_s


def svg_overwrite(filename, age_data, commit_data, star_data, repo_data, contrib_data, follower_data, loc_data):
    """
    تحديث عناصر الـ SVG مع محاذاة دقيقة (طول كل سطر = LINE_WIDTH حرف دايماً)
    """
    tree = etree.parse(filename)
    root = tree.getroot()

    MID_COL = 55  # العمود المشترك يلي يبلش فيه القسم الثاني بكل الأسطر المزدوجة

    # ---- سطر العمر (مفرد) ----
    age_dots, age_val = build_single_section('Uptime', age_data, LINE_WIDTH)
    find_and_replace(root, 'age_data_dots', age_dots)
    find_and_replace(root, 'age_data', age_val)

    # ---- سطر Repos / Contributed / Stars (مزدوج) ----
    repo_dots, repo_val, star_dots, star_val = build_dual_section(
        key1='Repos', val1=repo_data,
        middle_text=' {{Contributed: {}}} | '.format(fmt_value(contrib_data)),
        key2='Stars', val2=star_data,
        mid_col=MID_COL,
    )
    find_and_replace(root, 'repo_data_dots', repo_dots)
    find_and_replace(root, 'repo_data', repo_val)
    find_and_replace(root, 'contrib_data', fmt_value(contrib_data))
    find_and_replace(root, 'star_data_dots', star_dots)
    find_and_replace(root, 'star_data', star_val)

    # ---- سطر Commits / Followers (مزدوج) ----
    commit_dots, commit_val, follower_dots, follower_val = build_dual_section(
        key1='Commits', val1=commit_data,
        middle_text=' | ',
        key2='Followers', val2=follower_data,
        mid_col=MID_COL,
    )
    find_and_replace(root, 'commit_data_dots', commit_dots)
    find_and_replace(root, 'commit_data', commit_val)
    find_and_replace(root, 'follower_data_dots', follower_dots)
    find_and_replace(root, 'follower_data', follower_val)

    # ---- سطر Lines of Code (loc_data ...... (add++, dots del--)) ----
    LOC_MID_COL = 40  # عمود نهاية "Lines of Code on GitHub: <loc_data>"
    loc_total, loc_add, loc_del = loc_data[2], loc_data[0], loc_data[1]

    loc_dots, loc_val = build_single_section('Lines of Code on GitHub', loc_total, LOC_MID_COL)
    find_and_replace(root, 'loc_data_dots', loc_dots)
    find_and_replace(root, 'loc_data', loc_val)

    add_s = fmt_value(loc_add)
    del_s = fmt_value(loc_del)
    tail_prefix = ' ( '
    tail_add = add_s + '++, '
    tail_del_suffix = del_s + '-- )'
    fixed_tail_len = len(tail_prefix) + len(tail_add) + len(tail_del_suffix)
    del_dots_len = max(0, (LINE_WIDTH - LOC_MID_COL) - fixed_tail_len)
    find_and_replace(root, 'loc_add', add_s)
    find_and_replace(root, 'loc_del_dots', make_dots(del_dots_len))
    find_and_replace(root, 'loc_del', del_s)

    tree.write(filename, encoding='utf-8', xml_declaration=True)


def find_and_replace(root, element_id, new_text):
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = new_text


def commit_counter(comment_size):
    total_commits = 0
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    with open(filename, 'r') as f:
        data = f.readlines()
    data = data[comment_size:]
    for line in data:
        total_commits += int(line.split()[2])
    return total_commits


def user_getter(username):
    query_count('user_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            id
            createdAt
        }
    }'''
    request = simple_request(user_getter.__name__, query, {'login': username})
    return {'id': request.json()['data']['user']['id']}, request.json()['data']['user']['createdAt']


def follower_getter(username):
    query_count('follower_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            followers {
                totalCount
            }
        }
    }'''
    request = simple_request(follower_getter.__name__, query, {'login': username})
    return int(request.json()['data']['user']['followers']['totalCount'])


def query_count(funct_id):
    global QUERY_COUNT
    QUERY_COUNT[funct_id] += 1


def perf_counter(funct, *args):
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start


if __name__ == '__main__':
    # تم تعديل تاريخ الميلاد إلى 6 يناير 2006
    BIRTHDAY = datetime.datetime(2006, 1, 6)

    user_data, user_time = perf_counter(user_getter, USER_NAME)
    OWNER_ID, acc_date = user_data

    age_data, age_time = perf_counter(daily_readme, BIRTHDAY)
    total_loc, loc_time = perf_counter(loc_query, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'], 7)
    commit_data, commit_time = perf_counter(commit_counter, 7)
    star_data, star_time = perf_counter(graph_repos_stars, 'stars', ['OWNER'])
    repo_data, repo_time = perf_counter(graph_repos_stars, 'repos', ['OWNER'])
    contrib_data, contrib_time = perf_counter(graph_repos_stars, 'repos', ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
    follower_data, follower_time = perf_counter(follower_getter, USER_NAME)

    if os.path.exists('dark_mode.svg'):
        svg_overwrite('dark_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, total_loc)
    if os.path.exists('card.svg'):
        svg_overwrite('card.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, total_loc)

    print("SVG updated successfully!")
