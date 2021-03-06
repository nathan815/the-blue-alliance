import logging
import collections
import datetime
import json
import re
import urllib

from google.appengine.api import urlfetch
from google.appengine.ext import ndb

from consts.district_type import DistrictType
from consts.event_type import EventType

from models.event import Event
from models.match import Match
from models.sitevar import Sitevar

CHAMPIONSHIP_EVENTS_LABEL = 'Championship Event'
REGIONAL_EVENTS_LABEL = 'Week {}'
WEEKLESS_EVENTS_LABEL = 'Other Official Events'
OFFSEASON_EVENTS_LABEL = 'Offseason'
PRESEASON_EVENTS_LABEL = 'Preseason'


class EventHelper(object):
    """
    Helper class for Events.
    """
    @classmethod
    def alliance_selections_to_points(self, alliance_selections):
        team_points = {}
        for n, alliance in enumerate(alliance_selections):
            n += 1
            team_points[alliance['picks'][0]] = 17 - n
            team_points[alliance['picks'][1]] = 17 - n
            team_points[alliance['picks'][2]] = n
        return team_points

    @classmethod
    def groupByWeek(self, events):
        """
        Events should already be ordered by start_date
        """
        to_return = collections.OrderedDict()  # key: week_label, value: list of events

        current_week = 1
        week_start = None
        weekless_events = []
        offseason_events = []
        preseason_events = []
        for event in events:
            if event.official and event.event_type_enum in {EventType.CMP_DIVISION, EventType.CMP_FINALS}:
                if CHAMPIONSHIP_EVENTS_LABEL in to_return:
                    to_return[CHAMPIONSHIP_EVENTS_LABEL].append(event)
                else:
                    to_return[CHAMPIONSHIP_EVENTS_LABEL] = [event]
            elif event.official and event.event_type_enum in {EventType.REGIONAL, EventType.DISTRICT, EventType.DISTRICT_CMP}:
                if (event.start_date is None or
                   (event.start_date.month == 12 and event.start_date.day == 31)):
                    weekless_events.append(event)
                else:
                    if week_start is None:
                        diff_from_wed = (event.start_date.weekday() - 2) % 7  # 2 is Wednesday
                        week_start = event.start_date - datetime.timedelta(days=diff_from_wed)

                    if event.start_date >= week_start + datetime.timedelta(days=7):
                        current_week += 1
                        week_start += datetime.timedelta(days=7)

                    label = REGIONAL_EVENTS_LABEL.format(current_week)
                    if label in to_return:
                        to_return[label].append(event)
                    else:
                        to_return[label] = [event]
            elif event.event_type_enum == EventType.PRESEASON:
                preseason_events.append(event)
            else:
                # everything else is an offseason event
                offseason_events.append(event)

        # Add weekless + other events last
        if weekless_events:
            to_return[WEEKLESS_EVENTS_LABEL] = weekless_events
        if preseason_events:
            to_return[PRESEASON_EVENTS_LABEL] = preseason_events
        if offseason_events:
            to_return[OFFSEASON_EVENTS_LABEL] = offseason_events

        return to_return

    @classmethod
    def distantFutureIfNoStartDate(self, event):
        if not event.start_date:
            return datetime.datetime(2177, 1, 1, 1, 1, 1)
        else:
            return event.start_date

    @classmethod
    def distantFutureIfNoEndDate(self, event):
        if not event.end_date:
            return datetime.datetime(2177, 1, 1, 1, 1, 1)
        else:
            return event.end_date

    @classmethod
    def calculateTeamAvgScoreFromMatches(self, team_key, matches):
        """
        Given a team_key and some matches, find the team's average qual and elim score
        """
        all_qual_scores = []
        all_elim_scores = []
        for match in matches:
            if match.has_been_played:
                for alliance in match.alliances.values():
                    if team_key in alliance['teams']:
                        if match.comp_level in Match.ELIM_LEVELS:
                            all_elim_scores.append(alliance['score'])
                        else:
                            all_qual_scores.append(alliance['score'])
                        break
        qual_avg = float(sum(all_qual_scores)) / len(all_qual_scores) if all_qual_scores != [] else None
        elim_avg = float(sum(all_elim_scores)) / len(all_elim_scores) if all_elim_scores != [] else None
        return qual_avg, elim_avg, all_qual_scores, all_elim_scores

    @classmethod
    def calculateTeamWLTFromMatches(self, team_key, matches):
        """
        Given a team_key and some matches, find the Win Loss Tie.
        """
        wlt = {"win": 0, "loss": 0, "tie": 0}

        for match in matches:
            if match.has_been_played and match.winning_alliance is not None:
                if match.winning_alliance == "":
                    wlt["tie"] += 1
                elif team_key in match.alliances[match.winning_alliance]["teams"]:
                    wlt["win"] += 1
                else:
                    wlt["loss"] += 1
        return wlt

    @classmethod
    def getTeamWLT(self, team_key, event):
        """
        Given a team_key, and an event, find the team's Win Loss Tie.
        """
        match_keys = Match.query(Match.event == event.key, Match.team_key_names == team_key).fetch(500, keys_only=True)
        return self.calculateTeamWLTFromMatches(team_key, ndb.get_multi(match_keys))

    @classmethod
    def getWeekEvents(self):
        """
        Get events this week
        In general, if an event is currently going on, it shows up in this query
        An event shows up in this query iff:
        a) The event is within_a_day
        OR
        b) The event.start_date is on or within 4 days after the closest Wednesday
        """
        today = datetime.datetime.today()

        # Make sure all events to be returned are within range
        two_weeks_of_events_keys_future = Event.query().filter(
          Event.start_date >= (today - datetime.timedelta(days=7))).filter(
          Event.start_date <= (today + datetime.timedelta(days=7))).order(
          Event.start_date).fetch_async(50, keys_only=True)

        events = []
        diff_from_wed = 2 - today.weekday()  # 2 is Wednesday. diff_from_wed ranges from 3 to -3 (Monday thru Sunday)
        closest_wednesday = today + datetime.timedelta(days=diff_from_wed)

        two_weeks_of_event_futures = ndb.get_multi_async(two_weeks_of_events_keys_future.get_result())
        for event_future in two_weeks_of_event_futures:
            event = event_future.get_result()
            if event.within_a_day:
                events.append(event)
            else:
                offset = event.start_date.date() - closest_wednesday.date()
                if (offset == datetime.timedelta(0)) or (offset > datetime.timedelta(0) and offset < datetime.timedelta(4)):
                    events.append(event)

        EventHelper.sort_events(events)
        return events

    @classmethod
    def getEventsWithinADay(self):
        week_events = self.getWeekEvents()
        ret = []
        for event in week_events:
            if event.within_a_day:
                ret.append(event)
        return ret

    @classmethod
    def getShortName(self, name_str):
        """
        Extracts a short name like "Silicon Valley" from an event name like
        "Silicon Valley Regional sponsored by Google.org".

        See https://github.com/the-blue-alliance/the-blue-alliance-android/blob/master/android/src/test/java/com/thebluealliance/androidclient/test/helpers/EventHelperTest.java
        """
        # 2015+ districts
        re_string = '(?:' + '|'.join(DistrictType.abbrevs.keys()).upper() + ') District -(.+)'
        match = re.match(re_string, name_str)
        if match:
            partial = match.group(1).strip()
            match2 = re.match(r'(.+)Event', partial)
            if match2:
                return match2.group(1).strip()
            else:
                return partial

        # other districts and regionals
        match = re.match(r'\s*(?:MAR |PNW |)(?:FIRST Robotics|FRC|)(.+)(?:District|Regional|Region|State|Tournament|FRC|Field)\b', name_str)
        if match:
            short = match.group(1)
            match = re.match(r'(.+)(?:FIRST Robotics|FRC)', short)
            if match:
                return match.group(1).strip()
            else:
                return short.strip()

        return name_str.strip()

    @classmethod
    def get_timezone_id(cls, location, event_key):
        if location is None:
            logging.warning('Could not get timezone for event {} with no location!'.format(event_key))
            return None

        google_secrets = Sitevar.get_by_id("google.secrets")
        google_api_key = None
        if google_secrets is None:
            logging.warning("Missing sitevar: google.api_key. API calls rate limited by IP and may be over rate limit.")
        else:
            google_api_key = google_secrets.contents['api_key']

        # geocode request
        geocode_params = {
            'address': location,
            'sensor': 'false',
        }
        if google_api_key is not None:
            geocode_params['key'] = google_api_key
        geocode_url = 'https://maps.googleapis.com/maps/api/geocode/json?%s' % urllib.urlencode(geocode_params)
        try:
            geocode_result = urlfetch.fetch(geocode_url)
        except Exception, e:
            logging.warning('urlfetch for geocode request failed: {}'.format(geocode_url))
            logging.info(e)
            return None
        if geocode_result.status_code != 200:
            logging.warning('Geocoding for event {} failed with url {}'.format(event_key, geocode_url))
            return None
        geocode_dict = json.loads(geocode_result.content)
        if not geocode_dict['results']:
            logging.warning('No geocode results for event location: {}'.format(location))
            return None
        lat = geocode_dict['results'][0]['geometry']['location']['lat']
        lng = geocode_dict['results'][0]['geometry']['location']['lng']

        # timezone request
        tz_params = {
            'location': '%s,%s' % (lat, lng),
            'timestamp': 0,  # we only care about timeZoneId, which doesn't depend on timestamp
            'sensor': 'false',
        }
        if google_api_key is not None:
            tz_params['key'] = google_api_key
        tz_url = 'https://maps.googleapis.com/maps/api/timezone/json?%s' % urllib.urlencode(tz_params)
        try:
            tz_result = urlfetch.fetch(tz_url)
        except Exception, e:
            logging.warning('urlfetch for timezone request failed: {}'.format(tz_url))
            logging.info(e)
            return None
        if tz_result.status_code != 200:
            logging.warning('TZ lookup for (lat, lng) failed! ({}, {})'.format(lat, lng))
            return None
        tz_dict = json.loads(tz_result.content)
        if 'timeZoneId' not in tz_dict:
            logging.warning('No timeZoneId for (lat, lng)'.format(lat, lng))
            return None
        return tz_dict['timeZoneId']

    @classmethod
    def parseDistrictName(cls, district_name_str):
        return DistrictType.names.get(district_name_str, DistrictType.NO_DISTRICT)

    @classmethod
    def parseEventType(self, event_type_str):
        """
        Given an event_type_str from USFIRST, return the proper event type
        Examples:
        'Regional' -> EventType.REGIONAL
        'District' -> EventType.DISTRICT
        'District Championship' -> EventType.DISTRICT_CMP
        'MI FRC State Championship' -> EventType.DISTRICT_CMP
        'Championship Finals' -> EventType.CMP_FINALS
        'Championship' -> EventType.CMP_FINALS
        """
        event_type_str = event_type_str.lower()

        # Easy to parse
        if 'regional' in event_type_str:
            return EventType.REGIONAL
        elif 'offseason' in event_type_str:
            return EventType.OFFSEASON
        elif 'preseason' in event_type_str:
            return EventType.PRESEASON

        # Districts have multiple names
        if ('district' in event_type_str) or ('state' in event_type_str)\
           or ('region' in event_type_str) or ('qualif' in event_type_str):
            if 'championship' in event_type_str:
                return EventType.DISTRICT_CMP
            else:
                return EventType.DISTRICT

        # Everything else with 'champ' should be a Championship event
        if 'champ' in event_type_str:
            if 'division' in event_type_str:
                return EventType.CMP_DIVISION
            else:
                return EventType.CMP_FINALS

        # An event slipped through!
        logging.warn("Event type '{}' not recognized!".format(event_type_str))
        return EventType.UNLABLED

    @classmethod
    def sort_events(cls, events):
        """
        Sorts by start date then end date
        Sort is stable
        """
        events.sort(key=EventHelper.distantFutureIfNoStartDate)
        events.sort(key=EventHelper.distantFutureIfNoEndDate)
