"""
A spider that can crawl an Open edX instance.
"""
import os
import re
import json
from datetime import datetime
# urlparse library depends on Python version
try:
    from urllib.parse import urlparse
except ImportError:
    from urlparse import urlparse
from path import Path
import yaml
import requests
from urlobject import URLObject
import scrapy
from scrapy.spiders import CrawlSpider, Rule
from scrapy.linkextractors import LinkExtractor
from scrapy.spidermiddlewares.httperror import HttpError
from twisted.internet.error import DNSLookupError
from victor_bot_scraper.items import VictorBotScraperItem

LOGIN_HTML_PATH = "/login"
LOGIN_API_PATH = "/user_api/v1/account/login_session/"
AUTO_AUTH_PATH = "/auto_auth"
COURSE_BLOCKS_API_PATH = "/api/courses/v1/blocks/"
LOGIN_FAILURE_MSG = "We couldn't sign you in."


def get_csrf_token(response):
    """
    Extract the CSRF token out of the "Set-Cookie" header of a response.
    """
    cookie_headers = [
        h.decode('ascii') for h in response.headers.getlist("Set-Cookie")
    ]
    if not cookie_headers:
        return None
    csrf_headers = [
        h for h in cookie_headers if h.startswith("csrftoken=")
    ]
    if not csrf_headers:
        return None
    match = re.match("csrftoken=([^ ;]+);", csrf_headers[-1])
    return match.group(1)

class QuestionFinder(CrawlSpider):
    "A Scrapy spider that can crawl an Open edX instance."
    name = 'questionFinder'

    rules = (
        Rule(
            LinkExtractor(
                deny=[
                    # don't crawl logout links
                    r"/logout/",
                    # don't crawl xblock links
                    r"://[^/]+/xblock/",
                    # don't crawl anything that returns an archive
                    r"\?_accept=application/x-tgz",
                ],
                unique=True,
            ),
            callback='parse_item',
            follow=True,
        ),
    )

    def __init__(
            self,
            domain="jzoldak.sandbox.edx.org",
            email="myoungstrom@edx.org",
            password="victor",
            http_user=None,
            http_pass=None,
            course_key="course-v1:edX+Test101+course",
            data_dir="data",
        ):  # noqa
        super(QuestionFinder, self).__init__()

        self.login_email = email
        self.login_password = password
        self.domain = domain
        self.course_key = course_key
        self.http_user = http_user
        self.http_pass = http_pass
        self.data_dir = os.path.abspath(os.path.expanduser(data_dir))

        # set start URL based on course_key, which is the test course by default
        api_url = (
            URLObject("http://")
                .with_hostname(self.domain)
                .with_path(COURSE_BLOCKS_API_PATH)
                .set_query_params(
                    course_id=self.course_key,
                    depth="all",
                    all_blocks="true",
                )
            )
        self.start_urls = ["https://jzoldak.sandbox.edx.org/courses/course-v1:edX+Test101+course/info"]
        self.allowed_domains = [domain]

    def handle_error(self, failure):
        """
        Provides basic error information for bad requests.
        If the error was an HttpError or DNSLookupError, it
        prints more specific information.
        """
        self.logger.error(repr(failure))

        if failure.check(HttpError):
            response = failure.value.response
            self.logger.error('HttpError on %s', response.url)
            self.logger.error('HttpError Code: %s', response.status)
            if response.status in (401, 403):
                # If the error is from invalid login, tell the user
                self.logger.error(
                    "Credentials failed. Either add/update the current credentials "
                    "or remove them to enable auto auth"
                )
        elif failure.check(DNSLookupError):
            request = failure.request
            self.logger.error('DNSLookupError on %s', request.url)

    def start_requests(self):

        if self.login_email and self.login_password:
            login_url = (
                URLObject("http://")
                .with_hostname(self.domain)
                .with_path(LOGIN_HTML_PATH)
            )
            yield scrapy.Request(
                login_url,
                callback=self.after_initial_csrf,
                errback=self.handle_error
            )
        else:
            self.logger.info(
                "Please enter valid email/password combination"
            )

    def after_initial_csrf(self, response):
        """
        This method is called *only* if the crawler is started with an
        email and password combination.
        In order to log in, we need a CSRF token from a GET request. This
        method takes the result of a GET request, extracts the CSRF token,
        and uses it to make a login request. The response to this login
        request will be handled by the `after_initial_login` method.
        """
        login_url = (
            URLObject("http://")
            .with_hostname(self.domain)
            .with_path(LOGIN_API_PATH)
        )
        credentials = {
            "email": self.login_email,
            "password": self.login_password,
        }
        headers = {
            b"X-CSRFToken": get_csrf_token(response),
        }
        yield scrapy.FormRequest(
            login_url,
            formdata=credentials,
            headers=headers,
            callback=self.after_initial_login,
            errback=self.handle_error
        )

    def after_initial_login(self, response):
        """
        This method is called *only* if the crawler is started with an
        email and password combination.
        It verifies that the login request was successful,
        and then generates requests from `self.start_urls`.
        """
        if LOGIN_FAILURE_MSG in response.text:
            self.logger.error(
                "Credentials failed. Either add/update the current credentials "
                "or remove them to enable auto auth"
            )
            return

        self.logger.info("successfully completed initial login")

        for url in self.start_urls:
        	yield self.make_requests_from_url(url)

    def parse_item(self, response):
        """
        Get basic information about a page, so that it can be passed to the
        `pa11y` tool for further testing.
        @url https://www.google.com/
        @returns items 1 1
        @returns requests 0 0
        @scrapes url request_headers accessed_at page_title
        """
        # if we got redirected to a login page, then login
        if URLObject(response.url).path == LOGIN_HTML_PATH:
            reqs = self.handle_unexpected_redirect_to_login_page(response)
            for req in reqs:
                yield req

        title = response.xpath("//title/text()").extract_first()
        if title:
            title = title.strip()

        item = VictorBotScraperItem(
			url=response.url,
            page_title=title,
        )

        yield item
        

    def handle_unexpected_redirect_to_login_page(self, response):
        """
        This method is called if the crawler has been unexpectedly logged out.
        If that happens, and the crawler requests a page that requires a
        logged-in user, the crawler will be redirected to a login page,
        with the originally-requested URL as the `next` query parameter.
        This method simply causes the crawler to log back in using the saved
        email and password credentials. We rely on the fact that the login
        page will redirect the user to the URL in the `next` query parameter
        if the login is successful -- this will allow the crawl to resume
        where it left off.
        This is method is very much like the `get_initial_login()` method,
        but the callback is `self.after_login` instead of
        `self.after_initial_login`.
        """
        next_url = URLObject(response.url).query_dict.get("next")
        login_url = (
            URLObject("http://")
            .with_hostname(self.domain)
            .with_path(LOGIN_API_PATH)
        )
        if next_url:
            login_url = login_url.set_query_param("next", next_url)

        credentials = {
            "email": self.login_email,
            "password": self.login_password,
        }
        headers = {
            b"X-CSRFToken": get_csrf_token(response),
        }
        yield scrapy.FormRequest(
            login_url,
            formdata=credentials,
            headers=headers,
            callback=self.after_login,
            errback=self.handle_error
        )

    def after_login(self, response):
        """
        This is very much like the `after_initial_login()` method, but
        it searches for links in the response instead of generating
        requests from `self.start_urls`.
        """
        # delegate to the `parse_item()` method, which handles normal responses.
        for item in self.parse_item(response):
            yield item