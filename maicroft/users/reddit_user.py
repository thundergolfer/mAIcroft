# -*- coding: utf-8 -*-
from __future__ import print_function

import calendar
import datetime
import json
import logging
import re
import time
from collections import Counter
from itertools import groupby

import pytz
import requests

from maicroft.intelligence.anti_sociality import Antisociality
from maicroft.maicroft_exceptions import NoDataError, UserNotFoundError
from maicroft.social_objects import Comment, Submission
from maicroft.social_info_extraction import load_attributes
from maicroft.subreddits import default_subs, ignore_text_subs, subreddits_dict
from maicroft.util import Util
from maicroft.text_parser import TextParser

try:
    from urlparse import urlparse
except (ImportError):
    from urllib.parse import urlparse


"""
Contains the RedditUser class, which builds a profile of a reddit
user, trying the extraction meaningful information from the content submitted
by them to reddit
"""

parser = TextParser()
logger = logging.getLogger(__name__)


class RedditUserEncoder(json.JSONEncoder):
    DATE_FORMAT = "%Y-%m-%d"
    TIME_FORMAT = "%H:%M:%S"

    def default(self, obj):
        if isinstance(obj, datetime.datetime):
            return obj.strftime("%s %s" % (self.DATE_FORMAT, self.TIME_FORMAT))
        elif isinstance(obj, Antisociality):
            return {
                "percentage_insults": obj.percentage_comments_that_are_insults(),
                "highest_likelihood_insulting_comment": obj.most_likely_insult
            }
        return super(RedditUserEncoder, self).default(obj)


class RedditUser:
    """
    Models a redditor object. Contains methods for processing
    comments and submissions.

    """

    # If user has posted in a sub 3 times or more, they are
    # probably interested in the topic.
    MIN_THRESHOLD = 3
    MIN_THRESHOLD_FOR_DEFAULT = 10
    HEADERS = {
        'User-Agent': 'MAIcroft v0.1 by /u/thundergolfer'
    }

    IMAGE_DOMAINS = ["imgur.com", "flickr.com"]
    VIDEO_DOMAINS = ["youtube.com", "youtu.be", "vimeo.com", "liveleak.com"]
    IMAGE_EXTENSIONS = ["jpg", "png", "gif", "bmp"]

    def __init__(self, username, json_data=None):
        # Populate username and about data
        self.username = username

        self.comments = []
        self.submissions = []

        if not json_data:
            # Retrieve about
            self.about = self.get_about()
            if not self.about:
                raise UserNotFoundError
            # Retrieve comments and submissions
            self.comments = self.get_comments()
            self.submissions = self.get_submissions()
        else:
            data = json.loads(json_data)
            self.about = {
                "created_utc": datetime.datetime.fromtimestamp(
                    data["about"]["created_utc"], tz=pytz.utc
                ),
                "link_karma": data["about"]["link_karma"],
                "comment_karma": data["about"]["comment_karma"],
                "name": data["about"]["name"],
                "reddit_id": data["about"]["id"],
                "is_mod": data["about"]["is_mod"]
            }
            for c in data["comments"]:
                self.comments.append(
                    Comment(
                        id=c["id"],
                        subreddit=c["subreddit"],
                        text=c["text"],
                        created_utc=c["created_utc"],
                        score=c["score"],
                        permalink=c["permalink"],
                        submission_id=c["submission_id"],
                        edited=c["edited"],
                        top_level=c["top_level"],
                        gilded=c["gilded"]
                    )
                )
            for s in data["submissions"]:
                self.submissions.append(
                    Submission(
                        id=s["id"],
                        subreddit=s["subreddit"],
                        text=s["text"],
                        created_utc=s["created_utc"],
                        score=s["score"],
                        permalink=s["permalink"],
                        url=s["url"],
                        title=s["title"],
                        is_self=s["is_self"],
                        gilded=s["gilded"],
                        domain=s["domain"]
                    )
                )

        self.username = self.about["name"]
        self.signup_date = self.about["created_utc"]
        self.link_karma = self.about["link_karma"]
        self.comment_karma = self.about["comment_karma"]
        self.reddit_id = self.about["reddit_id"]
        self.is_mod = self.about["is_mod"]

        # Initialize other properties
        self.today = datetime.datetime.now(tz=pytz.utc).date()

        start = self.signup_date.date()

        self.age_in_days = (self.today - start).days

        self.first_post_date = None

        self.earliest_comment = None
        self.latest_comment = None
        self.best_comment = None
        self.worst_comment = None

        self.earliest_submission = None
        self.latest_submission = None
        self.best_submission = None
        self.worst_submission = None

        self.metrics = {
            "date": [],
            "weekday": [],
            "hour": [],
            "subreddit": [],
            "heatmap": [],
            "recent_karma": [],
            "recent_posts": []
        }

        self.submissions_by_type = {
            "name": "All",
            "children": [
                {
                    "name": "Self",
                    "children": []
                },
                {
                    "name": "Image",
                    "children": []
                },
                {
                    "name": "Video",
                    "children": []
                },
                {
                    "name": "Other",
                    "children": []
                }
            ]
        }

        self.metrics["date"] = [
            {
                "date": (year, month),
                "comments": 0,
                "submissions": 0,
                "comment_karma": 0,
                "submission_karma": 0
            } for (year, month) in sorted(
                list(
                    set([
                        (
                            (self.today - datetime.timedelta(days=x)).year,
                            (self.today - datetime.timedelta(days=x)).month
                        ) for x in range(0, (self.today - start).days)
                    ])
                )
            )
        ]

        self.metrics["heatmap"] = [0] * 24 * 61
        self.metrics["recent_karma"] = [0] * 61
        self.metrics["recent_posts"] = [0] * 61

        self.metrics["hour"] = [
            {
                "hour": hour,
                "comments": 0,
                "submissions": 0,
                "comment_karma": 0,
                "submission_karma": 0
            } for hour in range(0, 24)
        ]

        self.metrics["weekday"] = [
            {
                "weekday": weekday,
                "comments": 0,
                "submissions": 0,
                "comment_karma": 0,
                "submission_karma": 0
            } for weekday in range(0, 7)
        ]

        self.genders = []
        self.orientations = []
        self.relationship_partners = []

        # Data that we are reasonably sure that *are* names of places.
        self.places_lived = []

        # Data that looks like it could be a place, but we're not sure.
        self.places_lived_extra = []

        # Data that we are reasonably sure that *are* names of places.
        self.places_grew_up = []

        # Data that looks like it could be a place, but we're not sure.
        self.places_grew_up_extra = []

        self.family_members = []
        self.pets = []

        self.attributes = []
        self.attributes_extra = []

        self.possessions = []
        self.possessions_extra = []

        self.actions = []
        self.actions_extra = []

        self.favorites = []
        self.sentiments = []
        self.anti_sociality = Antisociality()

        self.derived_attributes = {
            "family_members": [],
            "gadget": [],
            "gender": [],
            "locations": [],
            "orientation": [],
            "physical_characteristics": [],
            "political_view": [],
            "possessions": [],
            "religion and spirituality": [],
        }

        self.corpus = ""

        self.commented_dates = []
        self.submitted_dates = []

        self.lurk_period = None

        self.comments_gilded = 0
        self.submissions_gilded = 0

        self.process()

    def __str__(self):
        return str(self.results())

    def get_about(self):
        """
        Returns basic data about redditor.

        """
        url = r"http://www.reddit.com/user/%s/about.json" % self.username
        response = requests.get(url, headers=self.HEADERS)
        response_json = response.json()
        if "error" in response_json and response_json["error"] == 404:
            return None
        about = {
            "created_utc": datetime.datetime.fromtimestamp(
                response_json["data"]["created_utc"], tz=pytz.utc
            ),
            "link_karma": response_json["data"]["link_karma"],
            "comment_karma": response_json["data"]["comment_karma"],
            "name": response_json["data"]["name"],
            "reddit_id": response_json["data"]["id"],
            "is_mod": response_json["data"]["is_mod"]
        }
        return about

    def get_comments(self, limit=None):
        """
        Returns a list of redditor's comments.

        """

        comments = []
        more_comments = True
        after = None
        base_url = r"http://www.reddit.com/user/%s/comments/.json?limit=100" \
            % self.username
        url = base_url
        while more_comments:
            response = requests.get(url, headers=self.HEADERS)
            response_json = response.json()

            # TODO - Error handling for user not found (404) and
            # rate limiting (429) errors

            for child in response_json["data"]["children"]:
                id = child["data"]["id"]
                subreddit = child["data"]["subreddit"]
                text = child["data"]["body"]
                created_utc = child["data"]["created_utc"]
                score = child["data"]["score"]
                submission_id = child["data"]["link_id"].lower()[3:]
                edited = child["data"]["edited"]
                top_level = True if child["data"]["parent_id"].startswith("t3") else False
                gilded = child["data"]["gilded"]
                permalink = "http://www.reddit.com/r/{}/comments/{}/_/{}".format(subreddit, submission_id, id)

                comment = Comment(
                    id=id,
                    subreddit=subreddit,
                    text=text,
                    created_utc=created_utc,
                    score=score,
                    permalink=permalink,
                    submission_id=submission_id,
                    edited=edited,
                    top_level=top_level,
                    gilded=gilded
                )

                comments.append(comment)

            after = response_json["data"]["after"]

            if after:
                url = base_url + "&after={}".format(after)
                # reddit may rate limit if we don't wait for 2 seconds
                # between successive requests. If that happens,
                # uncomment and increase sleep time in the following line.
                time.sleep(0.5)
            else:
                more_comments = False

        return comments

    def get_submissions(self, limit=None):
        """
        Returns a list of redditor's submissions.

        """

        submissions = []
        more_submissions = True
        after = None
        base_url = r"http://www.reddit.com/user/%s/submitted/.json?limit=100" % self.username
        url = base_url
        while more_submissions:
            response = requests.get(url, headers=self.HEADERS)
            response_json = response.json()

            # TODO - Error handling for user not found (404) and
            # rate limiting (429) errors

            for child in response_json["data"]["children"]:
                id = child["data"]["id"]
                subreddit = child["data"]["subreddit"]
                text = child["data"]["selftext"]
                created_utc = child["data"]["created_utc"]
                score = child["data"]["score"]
                permalink = "http://www.reddit.com" + child["data"]["permalink"]
                url = child["data"]["url"].lower()
                title = child["data"]["title"]
                is_self = child["data"]["is_self"]
                gilded = child["data"]["gilded"]
                domain = child["data"]["domain"]

                submission = Submission(
                    id=id,
                    subreddit=subreddit,
                    text=text,
                    created_utc=created_utc,
                    score=score,
                    permalink=permalink,
                    url=url,
                    title=title,
                    is_self=is_self,
                    gilded=gilded,
                    domain=domain
                )

                submissions.append(submission)

            after = response_json["data"]["after"]

            if after:
                url = base_url + "&after={}".format(after)
                # reddit may rate limit if we don't wait for 2 seconds
                # between successive requests. If that happens,
                # uncomment and increase sleep time in the following line.
                time.sleep(0.5)
            else:
                more_submissions = False

        return submissions

    def process(self):
        """
        Retrieves redditor's comments and submissions and
        processes each of them.

        """
        if self.comments:
            self.process_comments()

        if self.submissions:
            self.process_submissions()

        if self.comments or self.submissions:
            self.derive_attributes()

    def process_comments(self):
        """
        Process list of redditor's comments.

        """

        if not self.comments:
            return

        self.earliest_comment = self.comments[-1]
        self.latest_comment = self.comments[0]

        self.best_comment = self.comments[0]
        self.worst_comment = self.comments[0]

        for comment in self.comments:
            self.process_comment(comment)

    def process_submissions(self):
        """
        Process list of redditor's submissions.

        """

        if not self.submissions:
            return

        self.earliest_submission = self.submissions[-1]
        self.latest_submission = self.submissions[0]

        self.best_submission = self.submissions[0]
        self.worst_submission = self.submissions[0]

        for submission in self.submissions:
            self.process_submission(submission)

    def process_comment(self, comment):
        """
        Process a single comment.

        * Updates metrics
        * Sanitizes and extracts chunks from comment.

        """
        logger.info('Processing comment: {}'.format(comment.id))

        # Sanitize comment text.
        text = Util.sanitize_text(comment.text)

        # Add comment text to corpus.
        self.corpus += text.lower()

        logger.info('Running insult analysis on comment')
        self.anti_sociality.update(text)

        comment_timestamp = datetime.datetime.fromtimestamp(
            comment.created_utc, tz=pytz.utc
        )

        self.commented_dates.append(comment_timestamp)
        self.comments_gilded += comment.gilded

        days_ago_60 = self.today - datetime.timedelta(60)
        if (comment_timestamp.date() - days_ago_60).days > 0:
            self.metrics["heatmap"][
                (comment_timestamp.date() - days_ago_60).days*24 +
                comment_timestamp.hour
            ] += 1
            self.metrics["recent_karma"][
                (comment_timestamp.date() - days_ago_60).days
            ] += comment.score
            self.metrics["recent_posts"][
                (comment_timestamp.date() - days_ago_60).days
            ] += 1

        # Update metrics
        for i, d in enumerate(self.metrics["date"]):
            if d["date"] == (
                comment_timestamp.date().year,
                comment_timestamp.date().month
            ):
                d["comments"] += 1
                d["comment_karma"] += comment.score
                self.metrics["date"][i] = d
                break

        for i, h in enumerate(self.metrics["hour"]):
            if h["hour"] == comment_timestamp.hour:
                h["comments"] += 1
                h["comment_karma"] += comment.score
                self.metrics["hour"][i] = h
                break

        for i, w in enumerate(self.metrics["weekday"]):
            if w["weekday"] == comment_timestamp.date().weekday():
                w["comments"] += 1
                w["comment_karma"] += comment.score
                self.metrics["weekday"][i] = w
                break

        if comment.score > self.best_comment.score:
            self.best_comment = comment
        elif comment.score < self.worst_comment.score:
            self.worst_comment = comment

        # If comment is in a subreddit in which comments/self text
        # are to be ignored (such as /r/jokes, /r/writingprompts, etc),
        # do not process it further.
        if comment.subreddit in ignore_text_subs:
            return False

        # If comment text does not contain "I" or "my", why even bother?
        if not re.search(r"\b(i|my)\b", text, re.I):
            return False

        # Now, this is a comment that needs to be processed.
        (chunks, sentiments) = parser.extract_chunks(text)
        self.sentiments += sentiments

        for chunk in chunks:
            self.load_attributes(chunk, comment)

        return True

    def process_submission(self, submission):
        """
        Process a single submission.

        * Updates metrics
        * Sanitizes and extracts chunks from self text.

        """

        if(submission.is_self):
            text = Util.sanitize_text(submission.text)
            self.corpus += text.lower()

        submission_timestamp = datetime.datetime.fromtimestamp(
            submission.created_utc, tz=pytz.utc
        )

        self.submitted_dates.append(submission_timestamp)
        self.submissions_gilded += submission.gilded

        days_ago_60 = self.today - datetime.timedelta(60)
        if (submission_timestamp.date() - days_ago_60).days > 0:
            self.metrics["heatmap"][
                ((submission_timestamp.date() - days_ago_60).days-1)*24 +
                submission_timestamp.hour
            ] += 1
            self.metrics["recent_karma"][
                (submission_timestamp.date() - days_ago_60).days
            ] += submission.score
            self.metrics["recent_posts"][
                (submission_timestamp.date() - days_ago_60).days
            ] += 1

        for i, d in enumerate(self.metrics["date"]):
            if d["date"] == (
                submission_timestamp.date().year,
                submission_timestamp.date().month
            ):
                d["submissions"] += 1
                d["submission_karma"] += submission.score
                self.metrics["date"][i] = d
                break

        for i, h in enumerate(self.metrics["hour"]):
            if h["hour"] == submission_timestamp.hour:
                h["submissions"] += 1
                h["submission_karma"] += submission.score
                self.metrics["hour"][i] = h
                break

        for i, w in enumerate(self.metrics["weekday"]):
            if w["weekday"] == submission_timestamp.date().weekday():
                w["submissions"] += 1
                w["submission_karma"] += submission.score
                self.metrics["weekday"][i] = w
                break

        submission_type = None
        submission_domain = None
        submission_url_path = urlparse(submission.url).path

        if submission.domain.startswith("self."):
            submission_type = "Self"
            submission_domain = submission.subreddit
        elif (
            submission_url_path.endswith(tuple(self.IMAGE_EXTENSIONS)) or
            submission.domain.endswith(tuple(self.IMAGE_DOMAINS))
        ):
            submission_type = "Image"
            submission_domain = submission.domain
        elif submission.domain.endswith(tuple(self.VIDEO_DOMAINS)):
            submission_type = "Video"
            submission_domain = submission.domain
        else:
            submission_type = "Other"
            submission_domain = submission.domain
        t = [
            x for x in self.submissions_by_type["children"]
            if x["name"] == submission_type
        ][0]
        d = (
            [x for x in t["children"] if x["name"] == submission_domain] or
            [None]
        )[0]
        if d:
            d["size"] += 1
        else:
            t["children"].append({
                "name": submission_domain,
                "size": 1
            })

        if submission.score > self.best_submission.score:
            self.best_submission = submission
        elif submission.score < self.worst_submission.score:
            self.worst_submission = submission

        # If submission is in a subreddit in which comments/self text
        # are to be ignored (such as /r/jokes, /r/writingprompts, etc),
        # do not process it further.
        if submission.subreddit in ignore_text_subs:
            return False

        # Only process self texts that contain "I" or "my"
        if not submission.is_self or not re.search(r"\b(i|my)\b", text, re.I):
            return False

        (chunks, sentiments) = parser.extract_chunks(text)
        self.sentiments += sentiments

        for chunk in chunks:
            self.load_attributes(chunk, submission)

        return True

    def load_attributes(self, chunk, post):
        load_attributes(self, chunk, post.permalink)

    def derive_attributes(self):
        """
        Derives attributes using activity data.

        """

        for name, count in self.commented_subreddits():
            subreddit = subreddits_dict[name] if name in subreddits_dict else None
            if (
                subreddit and subreddit["attribute"] and
                count >= self.MIN_THRESHOLD
            ):
                self.derived_attributes[subreddit["attribute"]].append(
                    subreddit["value"].lower()
                )

        for name, count in self.submitted_subreddits():
            subreddit = subreddits_dict[name] if name in subreddits_dict else None
            if (
                subreddit and subreddit["attribute"] and
                count >= self.MIN_THRESHOLD
            ):
                self.derived_attributes[subreddit["attribute"]].append(
                    subreddit["value"].lower()
                )

        # If someone mentions their wife,
        # they should be male, and vice-versa (?)
        if "wife" in [v for v, s in self.relationship_partners]:
            self.derived_attributes["gender"].append("male")
        elif "husband" in [v for v, s in self.relationship_partners]:
            self.derived_attributes["gender"].append("female")

        commented_dates = sorted(self.commented_dates)
        submitted_dates = sorted(self.submitted_dates)
        active_dates = sorted(self.commented_dates + self.submitted_dates)

        min_date = datetime.datetime(datetime.MINYEAR, 1, 1, tzinfo=pytz.utc)
        first_comment_date = \
            min(commented_dates) if commented_dates else min_date
        first_submission_date = \
            min(submitted_dates) if submitted_dates else min_date

        self.first_post_date = max(first_comment_date, first_submission_date)

        active_dates += [datetime.datetime.now(tz=pytz.utc)]
        commented_dates += [datetime.datetime.now(tz=pytz.utc)]
        submitted_dates += [datetime.datetime.now(tz=pytz.utc)]

        # Find the longest period of inactivity
        comment_lurk_period = max(
            [
                {
                    "from": calendar.timegm(d1.utctimetuple()),
                    "to": calendar.timegm(d2.utctimetuple()),
                    "days": (d2 - d1).seconds,
                } for d1, d2 in zip(
                    commented_dates[:-1], commented_dates[1:]
                )
            ], key=lambda x: x["days"]
        ) if len(commented_dates) > 1 else {"days":-1}

        submission_lurk_period = max(
            [
                {
                    "from": calendar.timegm(d1.utctimetuple()),
                    "to": calendar.timegm(d2.utctimetuple()),
                    "days": (d2 - d1).seconds,
                } for d1, d2 in zip(
                    submitted_dates[:-1], submitted_dates[1:]
                )
            ], key=lambda x: x["days"]
        ) if len(submitted_dates) > 1 else {"days": -1}

        post_lurk_period = max(
            [
                {
                    "from": calendar.timegm(d1.utctimetuple()),
                    "to": calendar.timegm(d2.utctimetuple()),
                    "days": (d2 - d1).seconds,
                } for d1, d2 in zip(
                    active_dates[:-1], active_dates[1:]
                )
            ], key=lambda x: x["days"]
        )

        self.lurk_period = min(
            [
                x for x in [
                    comment_lurk_period,
                    submission_lurk_period,
                    post_lurk_period
                ] if x["days"] >= 0
            ],
            key=lambda x: x["days"]
        )
        del self.lurk_period["days"]

    def commented_subreddits(self):
        """
        Returns a list of subreddits redditor has commented on.

        """

        return [
            (name, count) for (name, count) in Counter(
                [comment.subreddit for comment in self.comments]
            ).most_common()
        ]

    def submitted_subreddits(self):
        """
        Returns a list of subreddits redditor has submitted to.

        """

        return [
            (name, count) for (name, count) in Counter(
                [submission.subreddit for submission in self.submissions]
            ).most_common()
        ]

    def results(self):
        """
        Returns accumulated data as JSON.

        """

        # Redditor has no data?
        if not (self.comments or self.submissions):
            raise NoDataError

        # Format metrics
        metrics_date = []

        for d in self.metrics["date"]:
            metrics_date.append(
                {
                    "date": "%d-%02d-01" % (d["date"][0], d["date"][1]),
                    "comments": d["comments"],
                    "submissions": d["submissions"],
                    "posts": d["comments"] + d["submissions"],
                    "comment_karma": d["comment_karma"],
                    "submission_karma": d["submission_karma"],
                    "karma": d["comment_karma"] + d["submission_karma"]
                }
            )

        metrics_hour = []

        for h in self.metrics["hour"]:
            metrics_hour.append(
                {
                    "hour": h["hour"],
                    "comments": h["comments"],
                    "submissions": h["submissions"],
                    "posts": h["comments"] + h["submissions"],
                    "comment_karma": h["comment_karma"],
                    "submission_karma": h["submission_karma"],
                    "karma": h["comment_karma"] + h["submission_karma"]
                }
            )

        weekdays = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

        metrics_weekday = []

        for w in self.metrics["weekday"]:
            metrics_weekday.append(
                {
                    "weekday": weekdays[w["weekday"]],
                    "comments": w["comments"],
                    "submissions": w["submissions"],
                    "posts": w["comments"] + w["submissions"],
                    "comment_karma": w["comment_karma"],
                    "submission_karma": w["submission_karma"],
                    "karma": w["comment_karma"] + w["submission_karma"]
                }
            )

        metrics_subreddit = {
            "name": "All",
            "children": []
        }

        for (name, [comments, comment_karma]) in [
            (s, [sum(x) for x in zip(*[(1, r[1]) for r in group])])
                for s, group in groupby(
                    sorted(
                        [
                            (p.subreddit, p.score) for p in self.comments
                        ], key=lambda x: x[0]
                    ), lambda x: x[0]
                )
        ]:
            subreddit = subreddits_dict[name] \
                if name in subreddits_dict else None
            if subreddit and subreddit["topic_level1"] != "Other":
                topic_level1 = subreddit["topic_level1"]
            else:
                topic_level1 = "Other"

            level1 = (
                [
                    t for t in metrics_subreddit["children"] \
                        if t["name"] == topic_level1
                ] or [None]
            )[0]
            if level1:
                level1["children"].append(
                    {
                        "name": name,
                        "comments": comments,
                        "submissions": 0,
                        "posts": comments,
                        "comment_karma": comment_karma,
                        "submission_karma": 0,
                        "karma": comment_karma
                    }
                )
            else:
                metrics_subreddit["children"].append(
                    {
                        "name": topic_level1,
                        "children": [
                            {
                                "name": name,
                                "comments": comments,
                                "submissions": 0,
                                "posts": comments,
                                "comment_karma": comment_karma,
                                "submission_karma": 0,
                                "karma": comment_karma
                            }
                        ]
                    }
                )

        for (name, [submissions, submission_karma]) in [
            (s, [sum(x) for x in zip(*[(1, r[1]) for r in group])])
                for s, group in groupby(
                    sorted(
                        [
                            (p.subreddit, p.score) for p in self.submissions
                        ], key=lambda x: x[0]
                    ), lambda x: x[0]
                )
        ]:
            subreddit = subreddits_dict[name] \
                if name in subreddits_dict else None
            if subreddit and subreddit["topic_level1"] != "Other":
                topic_level1 = subreddit["topic_level1"]
            else:
                topic_level1 = "Other"
            level1 = (
                [
                    t for t in metrics_subreddit["children"]
                    if t["name"] == topic_level1
                ] or [None]
            )[0]
            if level1:
                sub_in_level1 = (
                    [
                        s for s in level1["children"] if s["name"] == name
                    ] or [None]
                )[0]
                if sub_in_level1:
                    sub_in_level1["submissions"] = submissions
                    sub_in_level1["submission_karma"] = submission_karma
                    sub_in_level1["posts"] += submissions
                    sub_in_level1["karma"] += submission_karma
                else:
                    level1["children"].append(
                        {
                            "name": name,
                            "comments": 0,
                            "submissions": submissions,
                            "posts": submissions,
                            "comment_karma": 0,
                            "submission_karma": submission_karma,
                            "karma": submission_karma
                        }
                    )
            else:
                metrics_subreddit["children"].append(
                    {
                        "name": topic_level1,
                        "children": [
                            {
                                "name": name,
                                "comments": 0,
                                "submissions": submissions,
                                "posts": submissions,
                                "comment_karma": 0,
                                "submission_karma": submission_karma,
                                "karma": submission_karma
                            }
                        ]
                    }
                )

        metrics_topic = {
            "name": "All",
            "children": []
        }

        # We need both topics (for Posts across topics) and
        # synopsis_topics (for Synopsis) because we want to include only
        # topics that meet the threshold limits in synopsis_topics
        synopsis_topics = []

        for name, count in Counter(
            [s.subreddit for s in self.submissions] +
            [c.subreddit for c in self.comments]
        ).most_common():
            if (
                name in default_subs and
                count >= self.MIN_THRESHOLD_FOR_DEFAULT
            ) or count >= self.MIN_THRESHOLD:
                subreddit = subreddits_dict[name] \
                    if name in subreddits_dict else None
                if subreddit:
                    topic = subreddit["topic_level1"]
                    if subreddit["topic_level2"]:
                        topic += ">" + subreddit["topic_level2"]
                    else:
                        topic += ">" + "Generic"
                    if subreddit["topic_level3"]:
                        topic += ">" + subreddit["topic_level3"]
                    else:
                        topic += ">" + "Generic"
                    synopsis_topics += [topic] * count

        topics = []

        for comment in self.comments:
            subreddit = subreddits_dict[comment.subreddit] \
                if comment.subreddit in subreddits_dict else None
            if subreddit and subreddit["topic_level1"] != "Other":
                topic = subreddit["topic_level1"]
                if subreddit["topic_level2"]:
                    topic += ">" + subreddit["topic_level2"]
                else:
                    topic += ">" + "Generic"
                if subreddit["topic_level3"]:
                    topic += ">" + subreddit["topic_level3"]
                else:
                    topic += ">" + "Generic"
                topics.append(topic)
            else:
                topics.append("Other")

        for submission in self.submissions:
            subreddit = subreddits_dict[submission.subreddit] \
                if submission.subreddit in subreddits_dict else None
            if subreddit and subreddit["topic_level1"] != "Other":
                topic = subreddit["topic_level1"]
                if subreddit["topic_level2"]:
                    topic += ">" + subreddit["topic_level2"]
                else:
                    topic += ">" + "Generic"
                if subreddit["topic_level3"]:
                    topic += ">" + subreddit["topic_level3"]
                else:
                    topic += ">" + "Generic"
                topics.append(topic)
            else:
                topics.append("Other")

        for topic, count in Counter(topics).most_common():
            level_topics = topic.split(">")
            current_node = metrics_topic
            for i, level_topic in enumerate(level_topics):
                children = current_node["children"]
                if i+1 < len(level_topics):
                    found_child = False
                    for child in children:
                        if child["name"] == level_topic:
                            child_node = child
                            found_child = True
                            break
                    if not found_child:
                        child_node = {
                            "name": level_topic,
                            "children": []
                        }
                        children.append(child_node)
                    current_node = child_node
                else:
                    child_node = {
                        "name": level_topic,
                        "size": count
                    }
                    children.append(child_node)

        common_words = [
            {
                "text": word,
                "size": count
            } for word, count in Counter(
                parser.common_words(self.corpus)
            ).most_common(200)
        ]
        total_word_count = parser.total_word_count(self.corpus)
        unique_word_count = parser.unique_word_count(self.corpus)

        # Let's use an average of 40 WPM
        hours_typed = round(total_word_count/(40.00*60.00), 2)

        gender = []
        for value, count in Counter(
            [value for value, source in self.genders]
        ).most_common(1):
            sources = [s for v, s in self.genders if v == value]
            gender.append(
                {
                    "value": value,
                    "count": count,
                    "sources": sources
                }
            )

        orientation = []
        for value, count in Counter(
            [value for value, source in self.orientations]
        ).most_common(1):
            sources = [s for v, s in self.orientations if v == value]
            orientation.append(
                {
                    "value": value,
                    "count": count,
                    "sources": sources
                }
            )

        relationship_partner = []
        for value, count in Counter(
            [value for value, source in self.relationship_partners]
        ).most_common(1):
            sources = [s for v, s in self.relationship_partners if v == value]
            relationship_partner.append(
                {
                    "value": value,
                    "count": count,
                    "sources": sources
                }
            )

        places_lived = []
        for value, count in Counter(
            [value for value, source in self.places_lived]
        ).most_common():
            sources = [s for v, s in self.places_lived if v == value]
            places_lived.append(
                {
                    "value": value,
                    "count": count,
                    "sources": sources
                }
            )

        places_lived_extra = []
        for value, count in Counter(
            [value for value, source in self.places_lived_extra]
        ).most_common():
            sources = [s for v, s in self.places_lived_extra if v == value]
            places_lived_extra.append(
                {
                    "value": value,
                    "count": count,
                    "sources": sources
                }
            )

        places_grew_up = []
        for value, count in Counter(
            [value for value, source in self.places_grew_up]
        ).most_common():
            sources = [s for v, s in self.places_grew_up if v == value]
            places_grew_up.append(
                {
                    "value": value,
                    "count": count,
                    "sources": sources
                }
            )

        places_grew_up_extra = []
        for value, count in Counter(
            [value for value, source in self.places_grew_up_extra]
        ).most_common():
            sources = [s for v, s in self.places_grew_up_extra if v == value]
            places_grew_up_extra.append(
                {
                    "value": value,
                    "count": count,
                    "sources": sources
                }
            )

        family_members = []
        for value, count in Counter(
            [value for value, source in self.family_members]
        ).most_common():
            sources = [s for v, s in self.family_members if v == value]
            family_members.append(
                {
                    "value": value,
                    "count": count,
                    "sources": sources
                }
            )

        pets = []
        for value, count in Counter(
            [value for value, source in self.pets]
        ).most_common():
            sources = [s for v, s in self.pets if v == value]
            pets.append(
                {
                    "value": value,
                    "count": count,
                    "sources": sources
                }
            )

        favorites = []
        for value, count in Counter(
            [value for value, source in self.favorites]
        ).most_common():
            sources = [s for v, s in self.favorites if v == value]
            favorites.append(
                {
                    "value": value,
                    "count": count,
                    "sources": sources
                }
            )

        attributes = []
        for value, count in Counter(
            [value for value, source in self.attributes]
        ).most_common():
            sources = [s for v, s in self.attributes if v == value]
            attributes.append(
                {
                    "value": value,
                    "count": count,
                    "sources": sources
                }
            )

        attributes_extra = []
        for value, count in Counter(
            [value for value, source in self.attributes_extra]
        ).most_common():
            sources = [s for v, s in self.attributes_extra if v == value]
            attributes_extra.append(
                {
                    "value": value,
                    "count": count,
                    "sources": sources
                }
            )

        possessions = []
        for value, count in Counter(
            [value for value, source in self.possessions]
        ).most_common():
            sources = [s for v, s in self.possessions if v == value]
            possessions.append(
                {
                    "value": value,
                    "count": count,
                    "sources": sources
                }
            )

        possessions_extra = []
        for value, count in Counter(
            [value for value, source in self.possessions_extra]
        ).most_common():
            sources = [s for v, s in self.possessions_extra if v == value]
            possessions_extra.append(
                {
                    "value": value,
                    "count": count,
                    "sources": sources
                }
            )

        actions = []
        for value, count in Counter(
            [value for value, source in self.actions]
        ).most_common():
            sources = [s for v, s in self.actions if v == value]
            actions.append(
                {
                    "value": value,
                    "count": count,
                    "sources": sources
                }
            )

        actions_extra = []
        for value, count in Counter(
            [value for value, source in self.actions_extra]
        ).most_common():
            sources = [s for v, s in self.actions_extra if v == value]
            actions_extra.append(
                {
                    "value": value,
                    "count": count,
                    "sources": sources
                }
            )

        synopsis = {}

        if gender:
            synopsis["gender"] = {
                "data": gender
            }

        if orientation:
            synopsis["orientation"] = {
                "data": orientation
            }

        if relationship_partner:
            synopsis["relationship_partner"] = {
                "data": relationship_partner
            }

        if places_lived:
            synopsis["places_lived"] = {
                "data": places_lived
            }

        if places_lived_extra:
            if "places_lived" in synopsis:
                synopsis["places_lived"].update(
                    {
                        "data_extra": places_lived_extra
                    }
                )
            else:
                synopsis["places_lived"] = {
                    "data_extra": places_lived_extra
                }

        if places_grew_up:
            synopsis["places_grew_up"] = {
                "data": places_grew_up
            }

        if places_grew_up_extra:
            if "places_grew_up" in synopsis:
                synopsis["places_grew_up"].update(
                    {
                        "data_extra": places_grew_up_extra
                    }
                )
            else:
                synopsis["places_grew_up"] = {
                    "data_extra": places_grew_up_extra
                }

        if family_members:
            synopsis["family_members"] = {
                "data": family_members
            }

        if pets:
            synopsis["pets"] = {
                "data": pets
            }

        if favorites:
            synopsis["favorites"] = {
                "data": favorites
            }

        if attributes:
            synopsis["attributes"] = {
                "data": attributes
            }

        if attributes_extra:
            if "attributes" in synopsis:
                synopsis["attributes"].update(
                    {
                        "data_extra": attributes_extra
                    }
                )
            else:
                synopsis["attributes"] = {
                    "data_extra": attributes_extra
                }

        if possessions:
            synopsis["possessions"] = {
                "data": possessions
            }

        if possessions_extra:
            if "possessions" in synopsis:
                synopsis["possessions"].update(
                    {
                        "data_extra": possessions_extra
                    }
                )
            else:
                synopsis["possessions"] = {
                    "data_extra": possessions_extra
                }

        ''' Will work on actions later
        if actions:
            synopsis["actions"] = {
                "data": actions
            }

        if actions_extra:
            if "actions" in synopsis:
                synopsis["actions"].update(
                    {
                        "data_extra": actions_extra
                    }
                )
            else:
                synopsis["actions"] = {
                    "data_extra": actions_extra
                }
        '''

        level1_topic_groups = [
            "business","entertainment", "gaming", "hobbies and interests", "lifestyle",
            "locations", "music", "science", "sports", "technology",
            "news and politics"
        ]

        level2_topic_groups = [
            "television", "books", "celebrities", # Entertainment
            "religion and spirituality", # Lifestyle
        ]

        exclude_topics = ["general", "drugs", "meta", "adult and nsfw", "other"]

        exclude_coalesced_topics = [
            "religion and spirituality", "more interests", "alternative"
        ]

        topic_min_levels = {
            "business": 2,
            "entertainment": 3,
            "gaming": 2,
            "hobbies and interests": 2,
            "lifestyle": 2,
            "locations": 3,
            "music": 2,
            "science": 2,
            "sports": 2,
            "technology": 2,
            "news and politics": 2
        }


        for topic, count in Counter(synopsis_topics).most_common():
            if count < self.MIN_THRESHOLD:
                continue
            level_topics = [
                x.lower() for x in topic.split(">") if x.lower() != "generic"
            ]
            key = None
            if level_topics[0] not in exclude_topics:
                m = 2
                if level_topics[0] in level1_topic_groups:
                    m = topic_min_levels[level_topics[0]]
                if (
                    len(level_topics) >= m and
                    level_topics[1] in level2_topic_groups and
                    level_topics[1] not in exclude_topics
                ):
                    key = level_topics[1]
                elif (
                    len(level_topics) >= m and
                    level_topics[1] not in exclude_topics
                ):
                    key = level_topics[0]
                elif level_topics[0] not in level1_topic_groups:
                    key = "other"
                coalesced_topic = Util.coalesce(level_topics).lower()
                if key and coalesced_topic not in exclude_coalesced_topics:
                    if key in synopsis:
                        if key not in ["gender", "religion and spirituality"]:
                            synopsis[key]["data"].append(
                                {
                                    "value": coalesced_topic,
                                    "count": count
                                }
                            )
                    else:
                        synopsis[key] = {
                            "data": [
                                {
                                    "value": coalesced_topic,
                                    "count": count
                                }
                            ]
                        }

        for k in {k: v for k, v in self.derived_attributes.items() if len(v)}:
            dd = [
                {
                    "value": v,
                    "count": c,
                    "sources": None
                } for v, c in Counter(self.derived_attributes[k]).most_common()
            ]
            if k in ["gender", "religion and spirituality"]:
                dd = dd[:1]
            if k in synopsis:
                synopsis[k].update(
                    {
                        "data_derived": dd
                    }
                )
            else:
                synopsis[k] = {
                    "data_derived": dd
                }

        computed_comment_karma = sum(
            [x["comment_karma"] for x in metrics_date]
        )
        computed_submission_karma = sum(
            [x["submission_karma"] for x in metrics_date]
        )

        hmin = min(self.metrics["heatmap"])*1.0 or 1.0
        hmax = max(self.metrics["heatmap"])*1.0
        if hmin < hmax:
            heatmap = ''.join(
                [
                    hex(
                        int(Util.scale(h, (hmin, hmax), (1, 15)))
                    )[2:] if h > 0 else "0" for h in self.metrics["heatmap"]
                ]
            )
        else:
            heatmap = "0" * 1464

        results = {
            "username": self.username,
            "version": 8,
            "metadata": {
                "reddit_id": self.reddit_id,
                "latest_comment_id": self.latest_comment.id \
                    if self.latest_comment else None,
                "latest_submission_id": self.latest_submission.id \
                    if self.latest_submission else None
            },
            "summary": {
                "signup_date": calendar.timegm(
                        self.signup_date.utctimetuple()
                    ),
                "first_post_date": calendar.timegm(
                        self.first_post_date.utctimetuple()
                    ),
                "lurk_period": self.lurk_period,
                "comments": {
                    "count": len(self.comments),
                    "gilded": self.comments_gilded,
                    "best": {
                        "text": self.best_comment.text \
                            if self.best_comment else None,
                        "permalink": self.best_comment.permalink \
                            if self.best_comment else None
                    },
                    "worst": {
                        "text": self.worst_comment.text \
                            if self.worst_comment else None,
                        "permalink": self.worst_comment.permalink \
                            if self.worst_comment else None
                    },
                    "all_time_karma": self.comment_karma,
                    "computed_karma": computed_comment_karma,
                    "average_karma": round(
                        computed_comment_karma/(len(self.comments) or 1), 2
                    ),
                    "total_word_count": total_word_count,
                    "unique_word_count": unique_word_count,
                    "hours_typed": hours_typed,
                    "karma_per_word": round(
                        computed_comment_karma/(total_word_count*1.00 or 1), 2
                    )
                },
                "submissions": {
                    "count": len(self.submissions),
                    "gilded": self.submissions_gilded,
                    "best": {
                        "title": self.best_submission.title \
                            if self.best_submission else None,
                        "permalink": self.best_submission.permalink \
                            if self.best_submission else None
                    },
                    "worst": {
                        "title": self.worst_submission.title \
                            if self.worst_submission else None,
                        "permalink": self.worst_submission.permalink \
                            if self.worst_submission else None
                    },
                    "all_time_karma": self.link_karma,
                    "computed_karma": computed_submission_karma,
                    "average_karma": round(
                        computed_submission_karma /
                        (len(self.submissions) or 1), 2
                    ),
                    "type_domain_breakdown": self.submissions_by_type
                }
            },
            "anti_sociality": self.anti_sociality,
            "synopsis": synopsis,
            "metrics": {
                "date": metrics_date,
                "hour": metrics_hour,
                "weekday": metrics_weekday,
                "subreddit": metrics_subreddit,
                "topic": metrics_topic,
                "common_words": common_words,
                "recent_activity_heatmap": heatmap,
                "recent_karma": self.metrics["recent_karma"],
                "recent_posts": self.metrics["recent_posts"]
            }
        }

        return json.dumps(results, cls=RedditUserEncoder)


if __name__ == '__main__':
    u = RedditUser('thundergolfer')
    print(u.about)
