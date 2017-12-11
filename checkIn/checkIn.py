# all the imports
import os
import hashlib
import hmac
import sys
import types
from flask import Flask, request, session, g, redirect, url_for, render_template, send_from_directory, abort, safe_join, send_file
from flask_bootstrap import Bootstrap
from flask_socketio import SocketIO, emit
import sqlalchemy as sa
from sqlalchemy.orm import relationship, joinedload, scoped_session, sessionmaker
from sqlalchemy.ext.declarative import declarative_base
from iitlookup import IITLookup
from collections import defaultdict

# TODO: consider using flask-login
# or maybe not, they don't seem to support forced reauthentication on 'fresh' logins

version = "0.8.0"

app = Flask(__name__, static_url_path='/static', static_folder='static') # create the application instance :)
socketio = SocketIO(app)
app.config.from_object(__name__)
app.config.from_pyfile('config.cfg')
app.config.from_envvar('FLASKR_SETTINGS', silent=True)

Bootstrap(app)

engine = sa.create_engine(app.config['DB'], pool_recycle=3600)
Base = declarative_base()


# New schema
class Location(Base):
    __tablename__ = 'locations'
    id = sa.Column(sa.Integer, primary_key=True, autoincrement=True)
    name = sa.Column(sa.String(length=50), nullable=False)
    secret = sa.Column(sa.Binary(length=16), nullable=False)
    salt = sa.Column(sa.Binary(length=16), nullable=False)

    def set_secret(self, secret):
        self.salt = os.urandom(16)
        # 100,000 rounds of sha256 w/ a random salt
        self.secret = hashlib.pbkdf2_hmac('sha256', secret, self.salt, 100000)

    def verify_secret(self, attempt):
        return hmac.compare_digest(self.secret, hashlib.pbkdf2_hmac('sha256', attempt, self.salt, 100000))

    def __repr__(self):
        return "<Location %s>" % self.name


class Type(Base):
    __tablename__ = 'types'
    id = sa.Column(sa.Integer, primary_key=True, autoincrement=True)
    level = sa.Column(sa.Integer, nullable=False)
    name = sa.Column(sa.String(length=50), nullable=False)
    location_id = sa.Column(sa.Integer, sa.ForeignKey('locations.id'), nullable=False)

    def __repr__(self):
        return "<Type %s>" % self.name


class Access(Base):
    __tablename__ = 'access'
    id = sa.Column(sa.Integer, primary_key=True, autoincrement=True)
    sid = sa.Column(sa.BigInteger, sa.ForeignKey('users.sid'))
    timeIn = sa.Column(sa.DateTime, nullable=False)
    timeOut = sa.Column(sa.DateTime, default=None)
    location_id = sa.Column(sa.Integer, sa.ForeignKey('locations.id'), nullable=False)

    user = relationship('User')
    location = relationship('Location')

    def __repr__(self):
        return "<Access %s(%s-%s)>" % (self.user.name, str(self.timeIn), str(self.timeOut))


class HawkCard(Base):
    __tablename__ = 'hawkcards'
    sid = sa.Column(sa.BigInteger, sa.ForeignKey('users.sid'))
    card = sa.Column(sa.BigInteger, primary_key=True)

    user = relationship('User')

    def __repr__(self):
        return "<HawkCard %d (A%d)>" % (self.card, self.sid)


class Machine(Base):
    __tablename__ = 'machines'
    id = sa.Column(sa.Integer, primary_key=True, autoincrement=True)
    name = sa.Column(sa.String(length=50))
    location_id = sa.Column(sa.Integer, sa.ForeignKey('locations.id'), nullable=False)

    location = relationship('Location')

    def __repr__(self):
        return "<Machine %s>" % self.name


class Training(Base):
    __tablename__ = 'safetyTraining'
    id = sa.Column(sa.Integer, primary_key=True, autoincrement=True)
    trainee_id = sa.Column(sa.BigInteger, sa.ForeignKey('users.sid'))
    trainer_id = sa.Column(sa.BigInteger, sa.ForeignKey('users.sid'))
    machine_id = sa.Column(sa.Integer, sa.ForeignKey('machines.id'))
    date = sa.Column(sa.DateTime)

    trainee = relationship('User', foreign_keys=[trainee_id], back_populates='trainings')
    trainer = relationship('User', foreign_keys=[trainer_id])
    machine = relationship('Machine', foreign_keys=[machine_id])

    def __repr__(self):
        return "<%s trained %s on %s, time=%s>" %\
               (self.trainee.name, self.trainer.name, self.machine.name, str(self.date))


class User(Base):
    __tablename__ = 'users'
    sid = sa.Column(sa.BigInteger, primary_key=True)
    name = sa.Column(sa.String(length=100), nullable=False)
    type_id = sa.Column(sa.Integer, sa.ForeignKey('types.id'))
    waiverSigned = sa.Column(sa.DateTime)
    photo = sa.Column(sa.String(length=100), default='')
    location_id = sa.Column(sa.INTEGER, sa.ForeignKey('locations.id'), nullable=False, primary_key=True)
    pin = sa.Column(sa.Binary(length=16))
    pin_salt = sa.Column(sa.Binary(length=16))

    def set_pin(self, pin):
        self.pin_salt = os.urandom(16)
        # 100,000 rounds of sha256 w/ a random salt
        self.pin = hashlib.pbkdf2_hmac('sha256', bytearray(pin, 'utf-8'), self.pin_salt, 100000)

    def verify_pin(self, attempt):
        digest = hashlib.pbkdf2_hmac('sha256', bytearray(attempt, 'utf-8'), self.pin_salt, 100000)
        return hmac.compare_digest(self.pin, digest)

    def __repr__(self):
        return "<Location %s>" % self.name

    type = relationship('Type')
    location = relationship('Location')
    trainings = relationship('Training', foreign_keys=[Training.trainee_id])


    def __repr__(self):
        return "<User A%d (%s)>" % (self.sid, self.name)


class AdminLog(Base):
    __tablename__ = 'adminLog'
    id = sa.Column(sa.BigInteger, primary_key=True, autoincrement=True)
    admin_id = sa.Column(sa.BigInteger, sa.ForeignKey('users.sid'))
    action = sa.Column(sa.String(length=50))
    target_id = sa.Column(sa.BigInteger, sa.ForeignKey('users.sid'))
    data = sa.Column(sa.Text)
    location_id = sa.Column(sa.Integer, sa.ForeignKey('locations.id'))

    admin = relationship('User', foreign_keys=[admin_id])
    target = relationship('User', foreign_keys=[target_id])
    location = relationship('Location')

    def __repr__(self):
        return "<AdminLog %s (%s) %s, data=%s>" % (self.admin.name, self.action, self.target.name, self.data)


class CardScan(Base):
    __tablename__ = 'scanLog'
    id = sa.Column(sa.BigInteger, primary_key=True, autoincrement=True)
    card_id = sa.Column(sa.BigInteger, sa.ForeignKey('hawkcards.card'), nullable=False)
    time = sa.Column(sa.DateTime)
    location_id = sa.Column(sa.Integer, sa.ForeignKey('locations.id'), nullable=False)

    card = relationship('HawkCard')
    location = relationship('Location')

    def __repr__(self):
        return "<CardScan %d at %s>" % (self.card, self.time)


db_session = scoped_session(sessionmaker(bind=engine))
Base.metadata.create_all(engine)


@app.before_request
def update_global_context():
    # TODO: remove this dirty hack
    session['location_id'] = 1

    db = db_session()
    in_lab = db.query(Access)\
        .filter_by(timeOut=None)\
        .all()
    
    g.students = [a.user for a in in_lab if a.user.type.level == 0]
    g.staff = [a.user for a in in_lab if a.user.type.level > 0]
    g.staff.sort(key=lambda x: x.type.level, reverse=True)
    g.admin = db.query(User).filter_by(sid=session['admin']).one_or_none()\
               if 'admin' in session else None
    g.version = version


@app.teardown_appcontext
def close_db(error):
    db_session.remove()


@app.route('/auth', methods=['GET', 'POST'])
def auth():
    if request.method == 'GET':
        db = db_session()
        locations = db.query(Location).all()
        return render_template('auth.html', locations=locations)
    else:
        return abort(400)


@app.route('/')
def root():
    return render_template('index.html')


@app.route('/card_read/<int:location_id>', methods=['GET', 'POST'])
def card_read(location_id):
    resp = 'Read success: Facility %s, card %s' % (request.form['facility'], request.form['cardnum'])
    db = db_session()
    dbcard = db.query(HawkCard)\
        .filter_by(card=request.form['cardnum'])\
        .one_or_none()
    user = dbcard.user if dbcard else None
    socketio.emit('scan', {
        'facility': request.form['facility'],
        'card': request.form['cardnum'],
        'location': location_id,
        'sid': user.sid if user else None,
        'name': user.name if user else None,
    })
    print(resp)
    return resp


@app.route('/checkout_button/<int:location_id>', methods=['POST'])
def checkout_button(location_id):
    db = db_session()
    
    card = db.query(HawkCard).filter_by(
        sid=request.args['sid']
    ).one_or_none()
    logEntry = CardScan(card_id=card.card, time=sa.func.now(), location_id=location_id)
    
    location = db.query(Location).filter_by(
        id=location_id
    ).one_or_none()

    db.add(logEntry)

    if not location:
        print("Location %d not found" % location_id)

    else:
        lastIn = db.query(Access)\
            .filter_by(location_id=location.id)\
            .filter_by(timeOut=None)\
            .filter_by(sid=card.sid)\
            .one_or_none()
        
        if lastIn:
            # user signing out
            print("User %s (card id %d) signed out at location %s (id %d)" % (
                card.user.name, card.card, location.name, location.id
            ))
            # sign user out and send to confirmation page
            lastIn.timeOut = sa.func.now()
    db.commit()

    # need to query again for active users now that it's changed
    update_global_context()

    return success('checkout')

@app.route('/index', methods=['GET'])
def index():
    return redirect('/')

success_messages = defaultdict(str)
success_messages.update({
    'login':   "You have been logged in.",
    'logout':  "You have been logged out.",
    'checkin': "You have checked in.",
    'checkout': "You have checked out."
})


@app.route('/success/<action>', methods=['GET'])
def success(action):
    return render_template('success.html', msg=success_messages[action])


def _login(request):
    error = None
    if request.method == 'POST':
        if (request.form['username'] != app.config['USERNAME']
           or request.form['password'] != app.config['PASSWORD']):
            error = 'Authentication failure'
        else:
            session['logged_in'] = True
    return error


# Admin authentication
@app.route('/admin/login', methods=['GET','POST'])
def admin_login():
    if not request.args.get('sid') and not request.args.get('card'):
        return render_template('admin/login_cardtap.html')
    else:
        if not request.args.get('sid') or not request.args.get('sid').isdigit():
            return render_template('admin/login_cardtap.html',
                                   error='This HawkCard is not registered!')

        # check to see if user has a pin
        db = db_session()
        user = db.query(User).filter_by(sid=request.args.get('sid')).one_or_none()

        if not user.pin and user.type.level > 0:
            session['admin'] = user.sid
            return render_template('admin/change_pin.html')
        elif user.type.level <= 0:
            return render_template('admin/login_cardtap.html',
                                   error='Insufficient permission! This incident will be reported.')

        return render_template('admin/login_pin.html',
                               sid=request.args.get('sid'))


@app.route('/admin/auth', methods=['POST'])
def admin_auth():
    # sanity checks
    db = db_session()
    # check for sufficient permission
    user = db.query(User).filter_by(sid=request.form['sid']).one_or_none()
    if user.type.level <= 0:
        return render_template('admin/login_cardtap.html',
                               error='Insufficient permission! This incident will be reported.')
    # check valid pin
    if not user.verify_pin(request.form['pin']):
        return render_template('admin/login_pin.html',
                               error='Invalid PIN!',
                               sid=request.form['sid'])
    # we good
    session['admin'] = user.sid
    return redirect('/admin')



@app.route('/admin/logout', methods=['GET'])
def admin_logout():
    session['admin'] = None
    return redirect('/success/logout')


@app.route('/admin/change_pin', methods=['GET', 'POST'])
def admin_change_pin():
    if request.method == 'GET':
        return render_template('admin/change_pin.html')
    else:
        db = db_session()
        user = db.query(User).filter_by(sid=session['admin']).one_or_none()
        user.set_pin(request.form['pin'])
        db.commit()
        return redirect('/admin')


@app.route('/admin/clear_lab', methods=['GET'])
def admin_clear_lab():
    if not session['admin']:
        return redirect('/admin/login')

    db = db_session()
    db.query(Access).filter_by(timeOut=None).update({"timeOut": sa.func.now()}, synchronize_session=False)
    db.commit()
    session['admin'] = None
    return redirect('/success/checkout')


# Admin flow
@app.route('/admin', methods=['GET'])
def admin_dash():
    if session['admin']:
        return render_template('admin/index.html')
    else:
        return redirect('/')


@app.route('/admin/lookup', methods=['GET'])
def admin_lookup():
    if not session['admin']:
        return redirect('/')

    db = db_session()
    query = db.query(User)

    sid = request.args.get('sid')
    if sid and sid != '':
        query = query.filter_by(sid=sid)

    name = request.args.get('name')
    machines = None
    types = None
    if name and name != '':
        query = query.filter(User.name.ilike(name + '%'))

    results = query.limit(20).all()

    if len(results) == 1:
        machines = db.query(Machine).filter_by(location_id=session['location_id']).all()
        # if found user has lower rank than admin user
        if results[0].type.level < g.admin.type.level:
            types = db.query(Type).filter_by(location_id=session['location_id'])\
                                  .filter(Type.level <= g.admin.type.level)\
                                  .all()

    return render_template('admin/lookup.html', results=results, machines=machines, types=types, error=request.args.get('error'))


@app.route('/admin/clear_waiver', methods=['GET'])
def admin_clear_waiver():
    if not session['admin']:
        return redirect('/')
    if not request.args.get('sid'):
        return redirect('/admin/lookup')

    db = db_session()
    user = db.query(User).filter_by(sid=request.args.get('sid'),
                                    location_id=session['location_id']).one_or_none()
    user.waiverSigned = None
    db.commit()
    return redirect('/admin/lookup?sid=' + str(user.sid))


@app.route('/admin/training/add', methods=['POST'])
def admin_add_training():
    if not session['admin']:
        return redirect('/')
    db = db_session()
    t = Training(trainee_id=int(request.form['student_id']),
                 trainer_id=int(session['admin']),
                 machine_id=int(request.form['machine']),
                 date=sa.func.now())
    db.add(t)
    db.commit()
    return redirect('/admin/lookup?sid=' + str(request.form['student_id']))


@app.route('/admin/training/remove')
def admin_remove_training():
    if not session['admin']:
        return redirect('/')
    db = db_session()
    training = db.query(Training).filter_by(id=request.args.get('id')).one_or_none()
    sid = 0
    if training:
        sid = training.trainee_id if training else None
        db.delete(training)
        db.commit()
    else:
        sid = request.args.get('sid')

    return redirect('/admin/lookup?sid=' + str(sid))


@app.route('/admin/type/set')
def admin_set_type():
    if not session['admin']:
        return redirect('/')
    db = db_session()
    type = db.query(Type).filter_by(id=request.args['tid'], location_id=session['location_id']).one_or_none()
    user = db.query(User).filter_by(sid=request.args['sid']).one_or_none()
    if not type:
        return redirect('/admin/lookup?sid=' + request.args['sid'] + "&error=Type does not exist.")
    if not user:
        return redirect('/admin/lookup?sid=' + request.args['sid'] + "&error=User does not exist.")
    elif g.admin.type.level < type.level:
        return redirect('/admin/lookup?sid=' + request.args['sid'] + "&error=You don't have permission to set that type.")
    elif user.type.level > g.admin.type.level:
        return redirect('/admin/lookup?sid=' + request.args['sid'] + "&error=You don't have permission to modify that user.")

    user.type_id = request.args['tid']
    db.commit()
    return redirect('/admin/lookup?sid=' + request.args['sid'])



@app.route('/waiver', methods=['GET'])
def waiver():
    if not request.args.get('agreed'):
        return render_template('waiver.html', sid=request.args.get('sid'))
    elif request.args.get('agreed') == 'true':
        db = db_session()
        db.add(Access(
            sid=request.args.get('sid'),
            location_id=session['location_id'],
            timeIn=sa.func.now(),
            timeOut=None
        ))
        user = db.query(User)\
            .filter_by(sid=request.args.get('sid'))\
            .one_or_none()
        if user:
            user.waiverSigned=sa.func.now()
        db.commit()
        return redirect('/success/checkin')
    else:
        # TODO: clear any active session
        return redirect('/')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'GET':
            resp=""    
            card_id=request.args.get('card_id')
            name=""
            sid=""
            #try:
            il = IITLookup(app.config['IITLOOKUPURL'],app.config['IITLOOKUPUSER'],app.config['IITLOOKUPPASS'])
            resp=il.nameIDByCard(request.args.get('card_id'))
            #except:
            #   print(sys.exc_info()[0])
            if resp:        
                sid=resp['idnumber'][1:]
                name=("%s %s") % (resp['first_name'],resp['last_name'])
            return render_template('register.html',
                               sid=sid,
                               card_id=card_id,
                               name=name)

    elif request.method == 'POST':
        if request.form['name']=="" or int(request.form['sid'])<20000000:
            return render_template('register.html', sid=request.form['sid'],card_id=request.form['card_id'],name=request.form['name']) 
        db = db_session()
        newtype = db.query(Type)\
            .filter_by(location_id=session['location_id'])\
            .filter_by(level=0)\
            .one_or_none()

        db.add(User(sid=request.form['sid'],
                    name=request.form['name'].title(),
                    type_id=newtype.id,
                    waiverSigned=None,
                    location_id=session['location_id']))

        card = db.query(HawkCard)\
            .filter_by(card=request.form['card_id'])\
            .one_or_none()
        card.sid = request.form['sid']

        db.commit()
        return redirect(url_for('.waiver', sid=request.form['sid']))


@socketio.on('check in')
def check_in(data):
    db = db_session()
    data['card'] = int(data['card'])
    data['facility'] = int(data['facility'])
    data['location'] = int(data['location'])
    resp = ""

    logEntry = CardScan(card_id=data['card'], time=sa.func.now(), location_id=data['location'])

    card = db.query(HawkCard).filter_by(
        card=data['card']
    ).one_or_none()

    location = db.query(Location).filter_by(
        id=data['location']
    ).one_or_none()

    db.add(logEntry)

    if not location:
        resp = ("Location %d not found" % data['location'])

    if not card:
        # first time in lab
        resp = ("User for card id %d not found" % data['card'])

        db.add(HawkCard(sid=None, card=data['card']))

    if not card or not card.user:
        # send to registration page
        emit('go', {'to': url_for('.register', card_id=data['card'])})

    else:
        lastIn = db.query(Access) \
            .filter_by(location_id=location.id) \
            .filter_by(timeOut=None) \
            .filter_by(sid=card.sid) \
            .one_or_none()
        print(lastIn)
        if lastIn:
            # user signing out
            resp = ("User %s (card id %d) signed out at location %s (id %d)" % (
                card.user.name, data['card'], location.name, location.id
            ))
            # sign user out and send to confirmation page
            lastIn.timeOut = sa.func.now()
            emit('go', {'to': url_for('.success', action='checkout')})

        elif card.user.waiverSigned:
            # user signing in
            resp = ("User %s (card id %d) is cleared for entry at location %s (id %d)" % (
                card.user.name, data['card'], location.name, location.id
            ))
            # sign user in and send to confirmation page
            accessEntry = Access(sid=card.sid, timeIn=sa.func.now(), location_id=location.id)
            db.add(accessEntry)
            emit('go', {'to': url_for('.success', action='checkin')})

        else:
            # user has account but hasn't signed waiver
            resp = ("User %s (card id %d) needs to sign waiver at location %s (id %d)" % (
                card.user.name, data['card'],
                location.name, location.id
            ))
            # present waiver page
            emit('go', {'to': url_for('.waiver', sid=card.sid)})

    db.commit()
    print(resp)
    return resp


if __name__ == '__main__':
    app.jinja_env.auto_reload = True
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    socketio.run(app, host='0.0.0.0', debug=True)
