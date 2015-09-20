import sys, os, shutil, itertools, codecs
from cPickle import dump, load
from ConfigParser import SafeConfigParser
from collections import defaultdict
from requests.exceptions import HTTPError, ConnectionError
import okcupyd
from okcupyd.profile import Profile
from okcupyd.user import User
from okcupyd.session import Session, RateLimiter

import module_locator
BASE_FOLDER = module_locator.module_path()
PROFILE_FOLDER = os.path.join(BASE_FOLDER, 'profiles')
QUESTION_BACKUP_FOLDER = os.path.join(BASE_FOLDER, 'qbackup')
VALUATION_FOLDER = os.path.join(BASE_FOLDER, 'valuations')
USERNAME_FILE = os.path.join(BASE_FOLDER, 'users.txt')
DEACTIVATED_FILE = os.path.join(BASE_FOLDER, 'deactivated_users.txt')

def setup():
    config_file = os.path.join(BASE_FOLDER, 'config.ini')
    if not os.path.exists(config_file):
        print "The following info will be stored, unencyrpted, in %s"%(config_file)
        print "Your real user is your OKCupid account where you interact with others."
        print "Your shadow user is an OKCupid account used just to retrieve profile information."
        print "If you do not intend to create a shadow user, leave the corresponding fields blank."
        real_username = raw_input("Real username: ")
        real_password = raw_input("Real password: ")
        shadow_username = raw_input("Shadow username: ")
        shadow_password = raw_input("Shadow password: ")
        print "Adding a delay (e.g. 5 seconds) between requests may help prevent you from getting into trouble with OKCupid."
        while True:
            try:
                rate_limit = float(raw_input("Rate limit (in seconds): "))
            except ValueError:
                print "Please enter an integer or floating point number"
            else:
                break
        with open(config_file,'w') as F:
            F.write("[real_login]\n")
            F.write("username = %s\n"%(real_username))
            F.write("password = %s\n\n"%(real_password))
            F.write("[shadow_login]\n")
            F.write("username = %s\n"%(shadow_username))
            F.write("password = %s\n\n"%(shadow_password))
            F.write("[settings]\n")
            F.write("real_default = %s\n"%(1 if (len(shadow_username) == 0) else 0))
            F.write("rate_limit = %s\n"%(rate_limit))
    global REAL_USERNAME, REAL_PASSWORD, SHADOW_USERNAME, SHADOW_PASSWORD
    global REAL_DEFAULT, RATE_LIMIT
    parser = SafeConfigParser()
    parser.read(config_file)
    REAL_USERNAME = parser.get("real_login", "username")
    REAL_PASSWORD = parser.get("real_login", "password")
    SHADOW_USERNAME = parser.get("shadow_login", "username")
    SHADOW_PASSWORD = parser.get("shadow_login", "password")
    REAL_DEFAULT = bool(int(parser.get("settings", "real_default")))
    RATE_LIMIT = float(parser.get("settings", "rate_limit"))

    global real_backup, shadow_backup
    real_backup_file = os.path.join(QUESTION_BACKUP_FOLDER, REAL_USERNAME)
    if os.path.exists(real_backup_file):
        with open(real_backup_file) as F:
            real_backup = load(F)
    if SHADOW_USERNAME:
        shadow_backup_file = os.path.join(QUESTION_BACKUP_FOLDER, SHADOW_USERNAME)
        if os.path.exists(shadow_backup_file):
            with open(shadow_backup_file) as F:
                shadow_backup = load(F)

def login(real_user=None):
    if real_user is None: real_user = REAL_DEFAULT
    global rate_limiter
    try:
        test = rate_limiter.rate_limit
    except NameError:
        rate_limiter = RateLimiter(RATE_LIMIT)
    if real_user:
        return User(Session.login(REAL_USERNAME, REAL_PASSWORD, rate_limit=rate_limiter))
    else:
        return User(Session.login(SHADOW_USERNAME, SHADOW_PASSWORD, rate_limit=rate_limiter))

def save_to_file(obj, filename):
    # use tmp file in case interrupted.
    tmpfile = os.path.join(BASE_FOLDER, '_tmp')
    with open(tmpfile, 'w') as F:
        dump(obj, F)
    shutil.move(tmpfile, filename)

class StaticQuestion(object):
    def __init__(self, question):
        global question_counter
        question_counter += 1
        if question_counter % 20 == 0:
            print "... Question %s"%(question_counter)
        for prp in ("answered", "id", "text", "their_answer", "my_answer", "their_answer_matches",
                    "my_answer_matches", "their_note", "my_note"):
            setattr(self, prp, getattr(question, prp))

class StaticAnswerOption(object):
    def __init__(self, option):
        for prp in ("is_users", "is_match", "text", "id"):
            setattr(self, prp, getattr(option, prp))

class StaticUserQuestion(object):
    def __init__(self, question):
        global question_counter
        question_counter += 1
        if question_counter % 20 == 0:
            print "... Question %s"%(question_counter)
        for prp in ("answered", "id", "text", "explanation"):
            setattr(self, prp, getattr(question, prp))
        self.answer_options = [StaticAnswerOption(option) for option in question.answer_options]

class Valuations(object):
    # Stored separately from UserQuestions and QuestionBackups
    # so that it doesn't get overridden, and so that one
    # could have multiple valuations for
    # long and short term partners
    def __init__(self, categories=None, save_name='prefs'):
        self.save_name = save_name
        if os.path.exists(os.path.join(VALUATION_FOLDER, save_name)):
            self.load()
        else:
            if categories is None:
                categories = {'s': 'sex', 'm': 'mindset', 'p': 'physical', 'l': 'life'}
                # sex (frequency, openness, interests)
                # mindset (ethics, religion, politics, science)
                # physical (height, bodytype, picture rating)
                # life (activities, interests, staying up late)
            self.categories = categories

            # keys are question ids, values are categories above (e.g. sex)
            self.qcategory = {}

            # keys are question ids, values are dicts with keys option texts, integral values.
            # Ratings should be 0 centered, ie 0 indicates a standard value
            # for someone you would date. Nonzero values will be displayed
            #    - -10 : red
            # -9 - -5  : orange
            # -4 - -1  : yellow
            #  1 -  9  : blue
            # 10 +     : green
            self.qrating = defaultdict(dict)

            # keys are detail names (bodytype, etc), values are categories above
            self.dcategory = {}

            # keys are detail names (bodytype, etc), values are dicts with
            # keys possible detail options, integral values.
            # Same normalization as above.
            self.drating = defaultdict(dict)
            self.dopts = None

    def _get_cat(self):
        while True:
            cat = raw_input("Category? (n-skip, %s) > "%(", ".join("%s-%s"%(k,v) for k,v in self.categories.iteritems())))
            if cat == 'n' or cat in self.categories:
                return cat

    def _get_rating(self, text):
        while True:
            rating = raw_input(text.replace(u'\u2013','-') + u" (0-centered rating) > ")
            try:
                return int(rating)
            except ValueError:
                print "Integer value please."

    def rate_details(self, Q):
        # Q is a QuestionAnalyzer
        self.dopts = defaultdict(set)
        for prof in Q.profiles.itervalues():
            for det, value in prof.details.iteritems():
                if isinstance(value, list):
                    for v in value:
                        self.dopts[det].add(v)
                else:
                    self.dopts[det].add(value)
        for det, S in self.dopts.iteritems():
            self.dopts[det] = sorted(list(S))
        for det, L in self.dopts.iteritems():
            print det
            print "    " + "\n    ".join([str(a) for a in L])
            cat = self._get_cat()
            if cat == 'n': continue
            self.dcategory[det] = self.categories[cat]
            for v in L:
                rating = self._get_rating(str(v))
                self.drating[det][str(v)] = rating
        self.save()

    def _rate_question(self, question):
        print question.text
        print "    " + "\n    ".join([a.text for a in question.answer_options])
        cat = self._get_cat()
        if cat == 'n':
            self.qcategory[question.id] = None
            for a in question.answer_options:
                self.qrating[question.id][a.text] = 0
        else:
            self.qcategory[question.id] = self.categories[cat]
            for a in question.answer_options:
                rating = self._get_rating(a.text)
                self.qrating[question.id][a.text] = rating

    def rate_questions(self, QB, save_interval=5):
        counter = 0
        for importance in ('mandatory', 'very_important', 'somewhat_important',
                           'little_important', 'not_important'):
            for question in getattr(QB, importance):
                if (question.id in self.qcategory and
                    question.id in self.qrating and
                    len(self.qrating[question.id]) == len(question.answer_options)):
                    continue
                counter += 1
                if save_interval is not None and counter % save_interval == 0:
                    print "Saving...."
                    self.save()
                self._rate_question(question)
        self.save()

    def revise_question(self, QB, qtext):
        for importance in ('mandatory', 'very_important', 'somewhat_important',
                           'little_important', 'not_important'):
            for question in getattr(QB, importance):
                if question.text == qtext:
                    self._rate_question(question)
                    return
        print "Question not found!"

    def _rate(self, profile):
        ratings = defaultdict(int)
        answered = defaultdict(int)
        for question in profile.questions:
            if question.id in self.qcategory and question.their_answer is not None:
                cat = self.qcategory[question.id]
                if cat is not None:
                    answered[cat] += 1
                    ratings[cat] += self.qrating[question.id][question.their_answer]
        answered['overall'] = sum(v for v in answered.itervalues())
        ratings['overall'] = sum(v for v in ratings.itervalues())
        return ratings, answered

    def rate(self, username, Q=None):
        if Q is None:
            with open(os.path.join(PROFILE_FOLDER, username)) as F:
                profile = load(F)
        else:
            profile = Q.profiles[username]
        valued_answers = []
        for question in profile.questions:
            if question.id in self.qrating and question.their_answer is not None:
                rating = self.qrating[question.id][question.their_answer]
                if rating:
                    valued_answers.append((rating, question.text, question.their_answer))
        for rating, question, answer in sorted(valued_answers):
            print question
            print "%s : %s"%(rating, answer)
        ratings, answered = self._rate(profile)
        for cat in itertools.chain(self.categories.itervalues(),itertools.repeat("overall",1)):
            print "%s : %s / %s"%(cat, ratings[cat], answered[cat])

    def find_best_rated(self, Q, category='overall'):
        scored = []
        for profile in Q.profiles.itervalues():
            ratings, answered = self._rate(profile)
            scored.append((ratings[category], answered[category], profile))
        for rat, ans, prof in sorted(scored):
            print prof.username, "%s / %s"%(rat, ans)

    def save(self, name=None):
        if name is None: name = self.save_name
        save_to_file(self, os.path.join(VALUATION_FOLDER, name))

    def load(self, name=None):
        if name is None: name = self.save_name
        filename = os.path.join(VALUATION_FOLDER, name)
        with open(filename) as F:
            V = load(F)
        # We copy these in case the saved object is an old version of this class.
        for val in ('categories', 'qcategory', 'qrating', 'dcategory', 'drating', 'dopts'):
            setattr(self, val, getattr(V, val))

class StaticQuestionBackup(object):
    def __init__(self, me):
        for importance in ('mandatory', 'very_important', 'somewhat_important',
                           'little_important', 'not_important'):
            setattr(self, importance, [StaticUserQuestion(q) for q in getattr(me.questions, importance)])

class StaticPhotoInfo(object):
    def __init__(self, photo_info):
        for prp in ("id", "thumb_nail_left", "thumb_nail_top", "thumb_nail_right", "thumb_nail_bottom", "jpg_uri"):
            setattr(self, prp, getattr(photo_info, prp))

class StaticLookingFor(object):
    def __init__(self, looking_for):
        for prp in ("gentation", "single", "near_me", "kinds"):
            setattr(self, prp, getattr(looking_for, prp))
        self.ages = (looking_for.ages.min, looking_for.ages.max)

class StaticEssays(object):
    def __init__(self, essays):
        for prp in ('self_summary', 'my_life', 'good_at', 'people_first_notice',
                   'favorites', 'six_things', 'think_about', 'friday_night',
                   'private_admission', 'message_me_if'):
            setattr(self, prp, getattr(essays, prp))

class StaticProfile(object):
    def __init__(self, profile):
        self.username = profile.username
        self.details = profile.details.as_dict
        global question_counter
        question_counter = 0
        self.questions = [StaticQuestion(question) for question in profile.questions]
        self.photos = [StaticPhotoInfo(info) for info in profile.photo_infos]
        self.looking_for = StaticLookingFor(profile.looking_for)
        try:
            self.responds = profile.responds
        except IndexError:
            pass
        self.essays = StaticEssays(profile.essays)
        for prp in ('id', 'age', 'match_percentage', 'enemy_percentage', 'location',
                    'gender', 'orientation'):
            setattr(self, prp, getattr(profile, prp))

class QuestionAnalyzer(object):
    def __init__(self):
        self.profiles = {}
        for (dirpath, dirnames, filenames) in os.walk(PROFILE_FOLDER):
            for username in filenames:
                with open(os.path.join(dirpath, username)) as F:
                    self.profiles[username] = load(F)
        self.shadow_questions = {}
        self.real_questions = {}
        shadow_file = os.path.join(QUESTION_BACKUP_FOLDER, SHADOW_USERNAME)
        real_file = os.path.join(QUESTION_BACKUP_FOLDER, REAL_USERNAME)
        for D, file in [(self.shadow_questions, shadow_file), (self.real_questions, real_file)]:
            with open(file) as F:
                qbackup = load(F)
                for importance in ('mandatory', 'very_important', 'somewhat_important',
                                   'little_important', 'not_important'):
                    for question in getattr(qbackup, importance):
                        question.importance = importance
                        question.matches = []
                        for option in question.answer_options:
                            if option.is_users:
                                question.answer = option.text
                            if option.is_match:
                                question.matches.append(option.text)
                        D[question.id] = question
        self.questions = defaultdict(list)
        self.qstats = {}
        # my_bad, their_bad, answered
        self.answered = {}
        self.unanswered = {}
        for profile in self.profiles.itervalues():
            for question in profile.questions:
                question.username = profile.username
                self.questions[question.id].append(question)
                if question.my_answer is not None:
                    if question.id not in self.answered:
                        self.answered[question.id] = question.text
                    if question.id not in self.qstats:
                        self.qstats[question.id] = [0,0,0]
                    self.qstats[question.id][2] += 1
                    if not question.my_answer_matches:
                        self.qstats[question.id][0] += 1
                    if not question.their_answer_matches:
                        self.qstats[question.id][1] += 1
                elif (question.id not in self.unanswered
                      and question.id not in self.shadow_questions):
                    self.unanswered[question.id] = question.text

    def show_answer_mismatches(self):
        for id, real_question in self.real_questions.iteritems():
            if id in self.shadow_questions:
                shadow_question = self.shadow_questions[id]
                summary = ""
                if real_question.answer != shadow_question.answer:
                    summary += " ANSWER: %s (real) != %s (shadow)\n"%(real_question.answer, shadow_question.answer)
                if real_question.importance != shadow_question.importance:
                    summary += " IMPORTANCE: %s (real) != %s (shadow)\n"%(real_question.importance, shadow_question.importance)
                if sorted(real_question.matches) != sorted(shadow_question.matches):
                    summary += " MATCHES: %s (real) != %s (shadow)\n"%(" OR ".join(real_question.matches), " OR ".join(shadow_question.matches))
                if summary:
                    print real_question.text
                    print summary

    def shadow_unanswered(self):
        unanswered = []
        for id, Q in self.unanswered.iteritems():
            n = len(self.questions[id])
            unanswered.append((n, Q))
        for n, Q in reversed(sorted(unanswered)):
            print n, Q

    def n_answered_distribution(self):
        D = defaultdict(int)
        a = b = None
        for stats in self.qstats.itervalues():
            n = stats[2]
            D[n] += 1
            if a is None or n < a:
                a = n
            if b is None or n > b:
                b = n
        for n in range(a, b + 1):
            print "%3s -- %s"%(n, D[n])

    def answer_summary(self, id):
        Q = self.answered[id]
        n = self.qstats[id][2]
        a = self.qstats[id][0]
        b = self.qstats[id][1]
        f = float(a+b) / n
        imp = self.shadow_questions[id].importance
        A = self.shadow_questions[id].answer
        return u"{:.2f} {:<3}{:<3}{:<4}{}\n          {:<19}{}".format(f, a, b, n, Q, imp, A)

    def help_reanswer(self, id):
        Q = self.answered[id]
        A = self.shadow_questions[id].answer
        others = " ".join(list(set(question.their_answer for question in self.questions[id] if question.their_answer is not None and question.their_answer_matches)))
        imp = self.shadow_questions[id].importance
        return u" {}\n  {}\n  {}\n  {}".format(Q, A, others, imp)

    def best_to_answer(self, n_cutoff=0):
        answered = []
        for id in self.answered.iterkeys():
            n = self.qstats[id][2]
            if n >= n_cutoff:
                a = self.qstats[id][0]
                b = self.qstats[id][1]
                f = float(a+b) / n
                answered.append((f, a, b, -n, id))
        for f, a, b, mn, id in sorted(answered):
            print self.answer_summary(id)

    def show_questions_to_answer(self, f_cutoff=0.06, n_cutoff=15, by_profile=True):
        reanswers = []
        for id in self.qstats.iterkeys():
            if id in self.real_questions:
                continue
            n = self.qstats[id][2]
            if n >= n_cutoff:
                a = self.qstats[id][0]
                b = self.qstats[id][1]
                f = float(a+b) / n
                if f < f_cutoff:
                    reanswers.append((self.answered[id], id))
        if by_profile:
            print_order = []
            unclaimed = [id for Q, id in reanswers]
            ids = defaultdict(list)
            while len(unclaimed) > 0:
                profiles = defaultdict(int)
                for id in unclaimed:
                    for question in self.questions[id]:
                        profiles[question.username] += 1
                        ids[question.username].append(id)
                frac = []
                bestf = 0
                bestuser = ""
                for username, n in profiles.iteritems():
                    f = float(n) / len(self.profiles[username].questions)
                    if f > bestf:
                        bestf = f
                        bestuser = username
                print bestuser, bestf
                usersqs = []
                new_unclaimed = []
                for id in unclaimed:
                    if id in ids[bestuser]:
                        usersqs.append((self.answered[id], id))
                    else:
                        new_unclaimed.append(id)
                unclaimed = new_unclaimed
                for Q, id in sorted(usersqs):
                    print Q
        for Q, id in sorted(reanswers):
            print self.help_reanswer(id)

    def check_status(self, qtext):
        for id, question in self.answered.iteritems():
            if question[:50] == qtext[:50]:
                print self.answer_summary(id)
                break

def save_profile(session, username, resume=False, mp_cutoff=None):
    global curprofile
    if not resume:
        curprofile = Profile(session, username)
    try:
        if mp_cutoff is not None and curprofile.match_percentage < mp_cutoff:
            print "%s does not have a high enough match percentage -- not saving"%username
            return curprofile
        staticprofile = StaticProfile(curprofile)
        with open(os.path.join(PROFILE_FOLDER, username), "w") as F:
            dump(StaticProfile(curprofile), F)
    except HTTPError:
        print "%s has been deactivated"%(username)
        with open(DEACTIVATED_FILE, "a") as F:
            F.write(username + "\n")
    return curprofile

def save_profiles(mp_cutoff = None, overwrite = False, username_file = USERNAME_FILE):
    shadow = login()
    with open(username_file) as user_file:
        users = user_file.readlines()
        for user in users:
            user = user.strip()
            if (not overwrite) and os.path.exists(os.path.join(PROFILE_FOLDER, user)):
                continue
            resume = False
            try:
                if curprofile.username == user:
                    resume = True
            except NameError:
                pass
            print "Saving", user
            save_profile(shadow._session, user, resume=resume, mp_cutoff=mp_cutoff)

def write_username_file(profile_fetchable, username_file=USERNAME_FILE):
    if os.path.exists(username_file):
        print "WARNING: this action will overwrite the existing username file."
        proceed = raw_input("Do you want to proceed? y/[n] ")
        if proceed != "y":
            return
    with open(username_file,'w') as F:
        try:
            for i, profile in enumerate(profile_fetchable):
                F.write(profile.username + "\n")
                if i % 20 == 19:
                    print "%s usernames written"%(i+1)
        except HTTPError:
            pass

def new_users(new_userfile, old_userfile):
    with open(new_userfile) as F:
        new_users = F.readlines()
    with open(old_userfile) as F:
        old_users = F.readlines()
    actually_new = []
    for user in new_users:
        if user not in old_users:
            actually_new.append(user.strip())
    return actually_new

def backup_user_questions(real=None):
    if real_user is None: real_user = REAL_DEFAULT
    user = login(real)
    global question_counter
    question_counter = 0
    print "Backing up questions for %s"%(REAL_USERNAME if real else SHADOW_USERNAME)
    with open(os.path.join(QUESTION_BACKUP_FOLDER, user.username), "w") as F:
        dump(StaticQuestionBackup(user), F)
