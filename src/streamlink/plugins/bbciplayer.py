from __future__ import print_function

import base64
import re
from functools import partial
from hashlib import sha1

from streamlink.plugin import Plugin, PluginOptions
from streamlink.plugin.api import http
from streamlink.plugin.api import validate
from streamlink.stream import HDSStream
from streamlink.stream import HLSStream
from streamlink.utils import parse_xml, parse_json


class BBCiPlayer(Plugin):
    url_re = re.compile(r"""https?://(?:www\.)?bbc.co.uk/iplayer/
        (
            episode/(?P<episode_id>\w+)|
            live/(?P<channel_name>\w+)
        )
    """, re.VERBOSE)
    vpid_re = re.compile(r'"ident_id"\s*:\s*"(\w+)"')
    tvip_re = re.compile(r'event_master_brand=(\w+?)&')
    account_locals_re = re.compile(r'window.bbcAccount.locals\s*=\s*(\{.*?});')
    swf_url = "http://emp.bbci.co.uk/emp/SMPf/1.18.3/StandardMediaPlayerChromelessFlash.swf"
    hash = base64.b64decode(b"N2RmZjc2NzFkMGM2OTdmZWRiMWQ5MDVkOWExMjE3MTk5MzhiOTJiZg==")
    api_url = ("http://open.live.bbc.co.uk/mediaselector/5/select/"
               "version/2.0/mediaset/{platform}/vpid/{vpid}/atk/{vpid_hash}/asn/1/")
    platforms = ("pc", "iptv-all")
    config_url = "http://www.bbc.co.uk/idcta/config"
    auth_url = "https://account.bbc.com/signin"

    config_schema = validate.Schema(
        validate.transform(parse_json),
        {
            "signin_url": validate.url(),
            "identity": {
                "cookieAgeDays": int,
                "accessTokenCookieName": validate.text,
                "idSignedInCookieName": validate.text
            }
         }
    )
    mediaselector_schema = validate.Schema(
        validate.transform(partial(parse_xml, ignore_ns=True)),
        validate.union({
            "hds": validate.xml_findall(".//media[@kind='video']//connection[@transferFormat='hds']"),
            "hls": validate.xml_findall(".//media[@kind='video']//connection[@transferFormat='hls']")
        }),
        {validate.text: validate.all(
            [validate.all(validate.getattr("attrib"), validate.get("href"))],
            validate.transform(lambda x: list(set(x)))  # unique
        )}
    )
    options = PluginOptions({
        "password": None,
        "username": None
    })

    @classmethod
    def can_handle_url(cls, url):
        return cls.url_re.match(url) is not None

    @classmethod
    def _hash_vpid(cls, vpid):
        return sha1(cls.hash + str(vpid).encode("utf8")).hexdigest()

    def find_vpid(self, url, res=None):
        self.logger.debug("Looking for vpid on {0}", url)
        # Use pre-fetched page if available
        res = res or http.get(url)
        m = self.vpid_re.search(res.text)
        return m and m.group(1)

    def find_tvip(self, url):
        self.logger.debug("Looking for tvip on {0}", url)
        res = http.get(url)
        m = self.tvip_re.search(res.text)
        return m and m.group(1)

    def mediaselector(self, vpid):
        for platform in self.platforms:
            url = self.api_url.format(vpid=vpid, vpid_hash=self._hash_vpid(vpid), platform=platform)
            stream_urls = http.get(url, schema=self.mediaselector_schema)
            for surl in stream_urls.get("hls"):
                for s in HLSStream.parse_variant_playlist(self.session, surl).items():
                    yield s
            for surl in stream_urls.get("hds"):
                for s in HDSStream.parse_manifest(self.session, surl).items():
                    yield s

    def login(self, ptrt_url, context="tvandiplayer"):
        # get the site config, to find the signin url
        config = http.get(self.config_url, params=dict(ptrt=ptrt_url), schema=self.config_schema)

        res = http.get(config["signin_url"],
                       params=dict(userOrigin=context, context=context),
                       headers={"Referer": self.url})
        m = self.account_locals_re.search(res.text)
        if m:
            auth_data = parse_json(m.group(1))
            res = http.post(self.auth_url,
                            params=dict(context=auth_data["userOrigin"],
                                        ptrt=auth_data["ptrt"]["value"],
                                        userOrigin=auth_data["userOrigin"],
                                        nonce=auth_data["nonce"]),
                            data=dict(jsEnabled="false", attempts=0, username=self.get_option("username"),
                                      password=self.get_option("password")))
            # redirects to ptrt_url on successful login
            if res.url == ptrt_url:
                return res
        else:
            self.logger.error("Could not authenticate, could not find the authentication nonce")

    def _get_streams(self):
        self.logger.info("A TV License is required to watch BBC iPlayer streams, see the BBC website for more "
                         "information: https://www.bbc.co.uk/iplayer/help/tvlicence")
        page_res = None
        if self.get_option("username"):
            page_res = self.login(self.url)
            if not page_res:
                self.logger.error("Could not authenticate, check your username and password")
                return

        m = self.url_re.match(self.url)
        episode_id = m.group("episode_id")
        channel_name = m.group("channel_name")

        if episode_id:
            self.logger.debug("Loading streams for episode: {0}", episode_id)
            vpid = self.find_vpid(self.url, res=page_res)
            if vpid:
                self.logger.debug("Found VPID: {0}", vpid)
                for s in self.mediaselector(vpid):
                    yield s
            else:
                self.logger.error("Could not find VPID for episode {0}", episode_id)
        elif channel_name:
            self.logger.debug("Loading stream for live channel: {0}", channel_name)
            tvip = self.find_tvip(self.url)
            if tvip:
                self.logger.debug("Found TVIP: {0}", tvip)
                for s in self.mediaselector(tvip):
                    yield s

__plugin__ = BBCiPlayer
