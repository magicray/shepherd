import os
import re
import cgi
import json
import yaml
import copy
import fcntl
import flask
import base64
import sqlite3
import pymysql
import hashlib
import functools

config = """
mysql:
    host: 127.0.0.1
    user: root
    password:

agents:
    127.0.0.1:
        key: sha1sum(ip:key)

logs:
    server: 127.0.0.1:5000
    dir: /tmp/logs

apps:
    100001:
        key: sha1sum(appid:key)
        pythonpath: /usr/bin/python
        hosts:
            127.0.0.1:
                workflows: 10
        pools:
            centralbox:
                - 127.0.0.1
"""

schema = """
create database if not exists shepherd;
use shepherd;

create table locks(
    sequence   bigint unsigned primary key auto_increment,
    lockname   char(64)        not null,
    appid      char(32)        not null,
    workerid   bigint unsigned not null,
    timestamp  timestamp default current_timestamp,
    unique(lockname, appid, workerid)
) engine=innodb;
create index lock1 on locks(appid, workerid, lockname);

create table workers(
    workerid     bigint unsigned primary key auto_increment,
    appid        char(32)  not null,
    state        char(16)  not null,
    status       longblob,
    continuation longblob,
    session      int unsigned not null default 0,
    timestamp    timestamp default current_timestamp,
    created      timestamp
) engine=innodb;

create table messages(
    msgid     bigint unsigned primary key auto_increment,
    appid     char(32) not null,
    workerid  bigint unsigned not null,
    senderid  bigint unsigned not null,
    pool      char(32) not null default 'default',
    state     char(16) not null,
    lock_ip   char(15),
    priority  tinyint unsigned not null default 128,
    code      char(64) not null,
    data      longblob,
    timestamp timestamp default current_timestamp
) engine=innodb;

create index msg1 on messages(timestamp, state, lock_ip, appid, pool);
create index msg2 on messages(appid, workerid, msgid);

create table counters(
    appid     char(32) primary key,
    count     bigint,
    timestamp timestamp default current_timestamp
) engine=innodb;
"""

conf_file = '/tmp/shepherd.yaml'

conf = None
clientip = None
appid = None
req = None

db_conn = None
db_cursor = None

application = flask.Flask(__name__)


class CustomException():
    def __init__(self, status, response):
        self.status = status
        self.response = response


def throw(status, response):
    raise CustomException(status, response)


def json_response(status, obj=None):
    obj, status = (status, obj) if obj else (status, 200)

    return flask.Response(json.dumps(obj,
                                     default=lambda o: str(o),
                                     indent=4,
                                     sort_keys=True),
                          status,
                          mimetype='application/json')


def html_table_response(table):
    return flask.Response(
        '<table class="logs" align="center" border="1">{0}</table>'.format(
            table), 200, mimetype='text/html')


def login_response():
    return flask.Response(
        '401 Not Authorized', 401,
        {'WWW-Authenticate': 'Basic realm="Login Required"'})


def query(sql, params=None):
    db_cursor.execute(sql, params)
    return db_cursor.fetchall()


def transaction(*args, **kwargs):
    def fun(f):
        @application.route(*args, **kwargs)
        @functools.wraps(f)
        def f1(*args, **kwargs):
            global db_conn
            global db_cursor
            global conf
            global clientip
            global appid
            global req

            clientip = flask.request.headers.get('X-Real-IP',
                                                 flask.request.remote_addr)
            if not flask.request.authorization:
                return login_response()

            appid = flask.request.authorization['username']
            password = flask.request.authorization['password']

            conf = yaml.load(open(conf_file))

            authSuccess = False
            appid = int(appid) if appid.isdigit() else appid

            if appid in conf['agents']:
                h = hashlib.sha1('{0}:{1}'.format(appid, password)).hexdigest()
                if h == conf['agents'][appid]['key']:
                    authSuccess = True

            if appid in conf['apps']:
                h = hashlib.sha1('{0}:{1}'.format(appid, password)).hexdigest()
                if h == conf['apps'][appid]['key']:
                    authSuccess = True
                elif clientip in conf['agents']:
                    h = hashlib.sha1('{0}:{1}'.format(
                        conf['agents'][clientip]['key'],
                        conf['apps'][appid]['key'])).hexdigest()
                    if h == password:
                        authSuccess = True

            if not authSuccess:
                return login_response()

            db_conn = pymysql.connect(conf['mysql']['host'],
                                      conf['mysql']['user'],
                                      conf['mysql']['password'],
                                      'shepherd')
            db_cursor = db_conn.cursor(pymysql.cursors.DictCursor)

            req = json.loads(flask.request.data) if flask.request.data else {}

            query("""insert into counters values(%s, 0, null)
                     on duplicate key update count=count+1""", (appid))

            try:
                response = f(*args, **kwargs)
                db_conn.commit()
                status = 200
            except CustomException as e:
                db_conn.rollback()
                status = e.status
                response = e.response
            except pymysql.err.InternalError as e:
                db_conn.rollback()
                status = 500
                response = str(type(e)) + ' : ' + str(e)
            except Exception as e:
                db_conn.rollback()
                status = 400
                response = str(type(e)) + ' : ' + str(e)

            db_cursor.close()
            db_conn.close()

            if type(response) is flask.Response:
                return response

            return json_response(response, status)
        return f1
    return fun


@transaction('/counters', methods=['GET'])
def counters_get():
    return query("select * from counters")


@transaction('/tasks/<tmpid>', methods=['GET'])
def tasks_appid_get(tmpid):
    tmpid = int(tmpid) if tmpid.isdigit() else tmpid
    if appid != tmpid:
        return login_response()

    msgs = query("""select workerid, pool, code, priority, timestamp
                    from messages
                    where timestamp < now() and
                          state='head' and
                          lock_ip is null and
                          appid=%s
                    order by timestamp, priority""", (appid))
    return html_table_response(''.join([
        ('<tr><td>{0}</td><td>{1}</td><td>{2}</td><td>{3}' +
         '</td><td>{4}</td></tr>').format(
             m['workerid'], m['pool'], m['code'], m['priority'],
             m['timestamp']) for m in msgs]))


@transaction('/tasks', methods=['GET'])
def tasks_get():
    msgs = query("""select appid, count(*) as count
                    from messages
                    where timestamp < now() and
                          state='head' and
                          lock_ip is null
                    group by appid""")
    return html_table_response(''.join([
        '<tr><td>{0}</td><td><a href="/tasks/{1}">{2}</a></td></tr>'.format(
            m['appid'], m['appid'], m['count']) for m in msgs]))


@transaction('/locks/<tmpid>', methods=['GET'])
def locks_appid_get(tmpid):
    tmpid = int(tmpid) if tmpid.isdigit() else tmpid
    if appid != tmpid:
        return login_response()

    msgs = query("""select lockname, locks.workerid, status, locks.timestamp
                    from locks, workers
                    where locks.workerid=workers.workerid and
                          locks.appid=%s
                    order by lockname, sequence""", (appid))
    return html_table_response(''.join([
        ('<tr><td>{0}</td><td>{1}</td><td>{2}</td><td>{3}</td>' +
         '<td><a href="/unlock/{1}/{0}">unlock</a></td></tr>').format(
             m['lockname'], m['workerid'], m['status'],
             m['timestamp']) for m in msgs]))


@transaction('/locks', methods=['GET'])
def locks_get():
    msgs = query("""select appid, count(*) as count
                    from locks
                    group by appid""")
    return html_table_response(''.join([
        '<tr><td>{0}</td><td><a href="/locks/{1}">{2}</a></td></tr>'.format(
            m['appid'], m['appid'], m['count']) for m in msgs]))


@transaction('/pending', methods=['GET'])
def pending_get():
    msgs = query("""select appid, pool, count(*) as count
                    from messages
                    where timestamp < now() and
                          state='head' and lock_ip is null
                    group by appid, pool""")

    allocation = dict()
    for m in msgs:
        appid = int(m['appid']) if m['appid'].isdigit() else m['appid']

        if appid not in conf['apps']:
            continue

        app = conf['apps'][appid]

        if 'default' == m['pool']:
            ip_list = conf['apps'][appid]['hosts'].keys()
        else:
            ip_list = conf['apps'][appid]['pools'].get(m['pool'], [])

        while m['count'] > 0:
            start_count = m['count']
            for ip in ip_list:
                allocation.setdefault(ip, dict()).setdefault(appid, 0)

                if allocation[ip][appid] < app['hosts'][ip]['workflows']:
                    allocation[ip][appid] += 1
                    m['count'] -= 1
                    if 0 == m['count']:
                        break
            if m['count'] == start_count:
                break

    return allocation


@transaction('/workers', methods=['POST'])
def worker_post():
    pool = req.get('pool', 'default')
    priority = req.get('priority', 128)

    if 'workflow' in req:
        req['data'] = dict(workflow=req['workflow'], input=req['data'])

    query("""insert into workers set appid=%s, state='active',
             continuation=%s, status='null', created=now()
          """, (appid, json.dumps(req['data'], indent=4, sort_keys=True)))
    workerid = query("select last_insert_id() as workerid")[0]['workerid']
    query("""insert into messages
             set workerid=%s, appid=%s, senderid=%s,
                 pool=%s, state='head', priority=%s, code='init'
          """, (workerid, appid, workerid, pool, priority))

    return dict(workerid=workerid)


@transaction('/workers/<workerids>', methods=['GET'])
def workers_get(workerids):
    log_server = conf['logs']['server']

    result = dict()
    for w in workerids.split(','):
        rows = query("""select state, status, session from workers
                        where workerid=%s and appid=%s""", (w, appid))

        if 1 == len(rows):
            result[w] = dict(state=rows[0]['state'],
                             status=json.loads(rows[0]['status']),
                             session=rows[0]['session'],
                             logs='http://{0}/logs/{1}'.format(log_server, w))

    return result


def mark_head(workerid):
    rows = query("select appid from workers where workerid=%s", (workerid))
    if 1 != len(rows):
        throw(400, 'INVALID_MSG_DESTINATION')

    msgid = query("""select msgid from messages
                     where appid=%s and workerid=%s
                     order by msgid limit 1""", (rows[0]['appid'], workerid))

    if len(msgid) > 0:
        query("""update messages set state='head'
                 where msgid=%s""", (msgid[0]['msgid']))


@transaction('/messages/<workerid>', methods=['POST'])
def messages_post(workerid):
    pool = req.get('pool', 'default')
    priority = req.get('priority', 128)
    data = req.get('data', None)
    delay = req.get('delay', 0)

    if data:
        data = json.dumps(data, indent=4, sort_keys=True)

    rows = query("select appid from workers where workerid=%s", (workerid))
    if (1 != len(rows)) or (rows[0]['appid'] != str(appid)):
        throw(400, 'INVALID_MSG_DESTINATION')

    query("""insert into messages
             set workerid=%s, appid=%s, senderid=%s,
                 pool=%s, state='queued', priority=%s, code=%s, data=%s,
                 timestamp=now()+interval %s second
          """, (workerid, appid, 0, pool, priority, req['code'], data, delay))

    mark_head(workerid)

    return 'OK'


@transaction('/unlock/<workerid>/<lockname>', methods=['GET'])
def unlock(workerid, lockname):
    req['workerid'] = workerid
    req['status'] = 'manually unlocked({0})'.format(lockname)
    req['continuation'] = 'manually unlocked({0})'.format(lockname)
    req['unlock'] = [lockname]
    req['pool'] = 'default'
    req['msgid'] = 0
    return commit_impl()


@transaction('/commit', methods=['POST'])
def commit():
    return commit_impl()


def commit_impl():
    pool = query("select pool from messages where msgid=%s", (req['msgid']))
    query("delete from messages where msgid=%s", (req['msgid']))

    if 'pool' in req:
        pool = req.get('pool')
    else:
        pool = pool[0]['pool']

    if 'continuation' not in req:
        if 'exception' in req:
            workflow_status = req['exception']
            workflow_state = 'exception'
        elif 'status' in req:
            workflow_status = req['status']
            workflow_state = 'done'
        else:
            workflow_status = 'unknown'
            workflow_state = 'exception'

        query("""delete from messages where appid=%s and workerid=%s""",
              (appid, req['workerid']))
        query("""update workers set status=%s, continuation=null, state=%s
                 where workerid=%s and appid=%s
              """, (json.dumps(workflow_status, indent=4, sort_keys=True),
                    workflow_state,
                    req['workerid'],
                    appid))
        return "OK"

    def insert_message(workerid, pool, code, data=None):
        if data:
            data = json.dumps(data, indent=4, sort_keys=True)

        rows = query("select appid from workers where workerid=%s", (workerid))
        if 1 != len(rows):
            throw(400, 'INVALID_MSG_DESTINATION')

        appid = rows[0]['appid']

        query("""insert into messages
                 set workerid=%s, appid=%s, senderid=%s,
                     pool=%s, state='queued', code=%s, data=%s
              """, (workerid, appid, req['workerid'], pool, code, data))

    def get_lock_holder(lockname):
        row = query("""select workerid from locks where lockname=%s and appid=%s
                       order by sequence limit 1""", (lockname, appid))
        if len(row) < 1:
            return None
        else:
            return row[0]['workerid']

    if 'lock' in req:
        for lockname in set(req['lock']):
            query("insert into locks set lockname=%s, appid=%s, workerid=%s",
                  (lockname, appid, req['workerid']))

        counter = 0
        for lockname in set(req['lock']):
            row = query("""select workerid from locks
                           where lockname=%s and appid=%s
                           order by sequence limit 1""", (lockname, appid))

            if row[0]['workerid'] == req['workerid']:
                counter += 1

        if len(set(req['lock'])) == counter:
            insert_message(req['workerid'], pool, 'locked')

    if 'unlock' in req:
        for lockname in set(req['unlock']):
            query("""delete from locks
                     where lockname=%s and appid=%s and workerid=%s
                  """, (lockname, appid, req['workerid']))

        to_be_unlocked = set()
        for lockname in set(req['unlock']):
            other_workerid = get_lock_holder(lockname)

            if other_workerid:
                locks = query("""select lockname from locks
                                 where appid=%s and workerid=%s
                              """, (appid, other_workerid))

                counter = 0
                for l in locks:
                    tmp_workerid = get_lock_holder(l['lockname'])
                    if other_workerid == tmp_workerid:
                        counter += 1

                if len(locks) == counter:
                    to_be_unlocked.add(other_workerid)

        for w in to_be_unlocked:
            insert_message(w, 'default', 'locked')
            mark_head(w)

    if 'message' in req:
        for workerid, msg in req['message'].iteritems():
            insert_message(workerid,
                           msg.get('pool', 'default'),
                           msg['code'],
                           msg.get('data', None))
            mark_head(workerid)

    if 'alarm' in req:
        if int(req['alarm']) < 1:
            req['alarm'] = 0

        query("""delete from messages
                 where appid=%s and workerid=%s and code='alarm'
              """, (appid, req['workerid']))

        query("""insert into messages
                 set workerid=%s, appid=%s, senderid=%s,
                     pool=%s, state='queued',
                     code='alarm', timestamp=now()+interval %s second
              """,
              (req['workerid'], appid, req['workerid'], pool, req['alarm']))

    mark_head(req['workerid'])

    query("update workers set status=%s, continuation=%s where workerid=%s",
          (json.dumps(req['status'], indent=4, sort_keys=True),
           json.dumps(req['continuation'], indent=4, sort_keys=True),
           req['workerid']))

    return 'OK'


@transaction('/lockmessage', methods=['POST'])
def lockmessage_post():
    pools = copy.deepcopy(conf['apps'][appid]['pools'])
    pools['default'] = conf['apps'][appid]['hosts'].keys()

    for pool in pools.keys():
        if clientip not in pools[pool]:
            continue

        rows = query("""select msgid, workerid, code, data, senderid
                        from messages
                        where timestamp < now() and state='head' and
                        appid=%s and pool=%s and lock_ip is null
                        order by priority limit 1""", (appid, pool))

        if len(rows) > 0:
            query("update messages set lock_ip=%s where msgid=%s",
                  (clientip, rows[0]['msgid']))
            query("""update workers set session=session+1
                     where workerid=%s""", (rows[0]['workerid']))

            worker = query("""select continuation, session from workers
                              where workerid=%s""", (rows[0]['workerid']))[0]

            result = dict(msgid=rows[0]['msgid'],
                          workerid=rows[0]['workerid'],
                          session=worker['session'],
                          continuation=json.loads(worker['continuation']),
                          code=rows[0]['code'],
                          senderid=rows[0]['senderid'],
                          pool=pool)

            if rows[0]['data']:
                result['data'] = json.loads(rows[0]['data'])

            return result

    return 'NOT_FOUND'

regex = re.compile('^\[(.+?) (\d+) (\d+) (\d{6}\.\d{6}\.\d{6}) (.+?)\] : ')


@application.route('/log/<logfile>/<size>', methods=['POST'])
def log_put(logfile, size):
    conf = yaml.load(open(conf_file))
    os.chdir(conf['logs']['dir'])

    logdir = flask.request.headers.get('X-Real-IP', flask.request.remote_addr)
    logfile = os.path.join(logdir, logfile)

    fd = None
    try:
        if not os.path.isdir(logdir):
            os.makedirs(logdir)

        fd = os.open(logfile, os.O_CREAT | os.O_WRONLY | os.O_APPEND)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        if os.fstat(fd).st_size == int(size):
            os.write(fd, flask.request.data)

        result = dict(size=os.fstat(fd).st_size)
    finally:
        os.close(fd)

    conn = sqlite3.connect('index.db')
    conn.execute("""create table if not exists offsets(thread text,
        session integer, timestamp text, logfile text,
        begin integer, end integer, primary key(thread, session))""")
    conn.execute("create index if not exists timestamp on offsets(timestamp)")
    conn.execute("""create table if not exists files(logfile text,
        processed integer, primary key(logfile))""")
    processed = conn.execute("select processed from files where logfile=?",
                             (logfile,)).fetchone()
    if processed:
        processed = processed[0]
    else:
        processed = 0
        conn.execute("insert into files values(?, 0)", (logfile,))
        conn.commit()
    conn.close()

    with open(logfile, 'r') as file:
        file.seek(processed)
        boffsets = dict()
        eoffsets = dict()
        for line in file:
            m = regex.match(line)
            if m:
                key = (m.group(1), m.group(2))
                if key not in boffsets:
                    boffsets[key] = (m.group(4), processed)
                eoffsets[key] = processed + len(line)
            processed += len(line)

    conn = sqlite3.connect('index.db')
    conn.executemany("insert or ignore into offsets values(?,?,?,?,?,0)",
                     [(k[0], k[1], v[0], logfile, v[1])
                         for k, v in boffsets.iteritems()])
    conn.executemany("update offsets set end=? where thread=? and session=?",
                     [(v, k[0], k[1]) for k, v in eoffsets.iteritems()])
    conn.execute("update files set processed=? where logfile=?",
                 (processed, logfile))
    conn.commit()
    conn.close()

    return json_response(result)


@application.route('/logs', methods=['GET'])
@application.route('/logs/<thread>', methods=['GET'])
@application.route('/logs/<thread>/sessions', methods=['GET'])
@application.route('/logs/<thread>/<session>', methods=['GET'])
def logs_get(thread=None, session=None):
    conf = yaml.load(open(conf_file))
    os.chdir(conf['logs']['dir'])

    begin = flask.request.args.get('begin', '0')
    end = flask.request.args.get('end', '9')
    limit = flask.request.args.get('limit', 25)

    conn = sqlite3.connect('index.db')

    if flask.request.path.startswith('/logs') and (thread is None):
        result = conn.execute("""select thread, max(timestamp), count(session)
            from offsets where timestamp > ? and timestamp < ?
            group by thread order by timestamp desc
            limit ?""", (begin, end, limit)).fetchall()
        conn.close()

        return html_table_response(''.join(['''<tr>
            <td><a href="{0}">{1}</a></td>
            <td><a href="{2}">{3} sessions</a></td>
            <td>{4}</td>
            </tr>'''.format('/logs/{0}'.format(r[0]), r[0],
                            '/logs/{0}/sessions'.format(r[0]), r[2],
                            r[1]) for r in result]))
    elif flask.request.path.endswith('/sessions'):
        result = conn.execute("""select session, timestamp from offsets
            where thread=? order by timestamp desc""", (thread,)).fetchall()
        conn.close()
        return html_table_response(''.join([
            '<tr><td><a href="{0}">{1}</a></td><td>{2}</td></tr>'.format(
                '/logs/{0}/{1}'.format(thread, r[0]), r[0], r[1])
            for r in result]))

    rows = conn.execute("""select session, logfile, begin, end
        from offsets where thread=?""", (thread,)).fetchall()
    conn.close()

    if session:
        sset = set()
        for l in [r.split('-') for r in session.split(',')]:
            r = (l[0], l[1]) if (len(l) > 1) else (l[0], l[0])
            for n in range(int(r[0]), int(r[1])+1):
                sset.add(n)
        sdict = dict([(x[0], (x[1], x[2], x[3]))
                     for x in rows if x[0] in sset])
    else:
        sdict = dict([(r[0], (r[1], r[2], r[3])) for r in rows])

    result = list()
    for s in sorted(sdict.keys()):
        logfile, begin, end = sdict[s]

        with open(logfile, 'r') as file:
            file.seek(begin)

            blobs = dict()
            for line in file:
                if begin > end:
                    break

                m = regex.match(line)
                if m and (thread == m.group(1)) and (str(s) == m.group(2)):
                    seq, timestamp, tag = m.group(3), m.group(4), m.group(5)
                    if 'BLOB' == tag:
                        hdr = '[{0} {1} {2} {3} {4}]'.format(
                            m.group(1), m.group(2), seq, timestamp, tag)
                        blobs[hashlib.md5(hdr).hexdigest()] = begin
                    else:
                        msg = cgi.escape(line[len(m.group(0)):])
                        find_pattern = '([-\w]+)&lt;&lt;(\w{32})&gt;&gt;'
                        for m in re.finditer(find_pattern, msg):
                            if m.group(2) in blobs:
                                msg = msg.replace(
                                    m.group(0),
                                    '<a href="/blob/{0}/{1}">{2}</a>'
                                    .format(logfile, blobs[m.group(2)],
                                            m.group(1)))
                        for m in re.finditer('&lt;&lt;(\w{32})&gt;&gt;', msg):
                            if m.group(1) in blobs:
                                msg = msg.replace(
                                    m.group(0),
                                    '<a href="/blob/{0}/{1}">blob</a>'
                                    .format(logfile, blobs[m.group(1)]))

                        tag = tag.replace(',', ' ') + ' SESSION-' + str(s)
                        result.append((timestamp, tag, msg))
                begin += len(line)

    return html_table_response(''.join([
        '<tr class="{0}"><td class="timestamp">{1}</td><td>{2}</td></tr>'
        .format(r[1], r[0], r[2]) for r in result]))


@application.route('/blob/<ip>/<logfile>/<offset>', methods=['GET'])
def blob_get(ip, logfile, offset):
    conf = yaml.load(open(conf_file))
    os.chdir(conf['logs']['dir'])

    with open(os.path.join(ip, logfile)) as file:
        file.seek(int(offset))

        line = file.readline()
        m = regex.match(line)
        if m:
            return flask.Response(base64.b64decode(line[len(m.group(0)):]),
                                  200, mimetype='text/plain')

    return flask.Response('NOT FOUND', 402, mimetype='text/plain')


@application.route('/config', methods=['GET'])
def config_get():
    conf = yaml.load(open(conf_file))
    c = copy.deepcopy(conf)

    if ('mysql' in c) and ('password' in c['mysql']):
        c['mysql']['password'] = '*****'

    c['agentip'] = flask.request.headers.get('X-Real-IP',
                                             flask.request.remote_addr)

    return json_response(c)


@application.route('/', methods=['GET'])
def index():
    return flask.Response('\n'.join([conf_file, config, schema]),
                          200, mimetype='text/plain')
