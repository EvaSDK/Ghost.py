# -*- coding: utf-8 -*-
import codecs
import logging
import os
import re
import sys
import time
import uuid
from contextlib import contextmanager
from functools import partial, wraps

from xvfbwrapper import Xvfb

from .bindings import (
    QApplication,
    QByteArray,
    QDateTime,
    QImage,
    QNetworkAccessManager,
    QNetworkCookie,
    QNetworkCookieJar,
    QNetworkDiskCache,
    QNetworkProxy,
    QNetworkRequest,
    QPainter,
    QPrinter,
    QRegion,
    QSize,
    QSsl,
    QSslConfiguration,
    QtCore,
    QtCriticalMsg,
    QtDebugMsg,
    QtFatalMsg,
    QtNetwork,
    QtWarningMsg,
    QtWebKit,
    QWebView,
    QWebPage,
    QUrl,
    BINDING,
    qInstallMsgHandler,
)

try:
    from cookielib import Cookie, CookieJar, LWPCookieJar
except ImportError:
    from http.cookiejar import Cookie, CookieJar, LWPCookieJar

__version__ = "0.2.3.post10"


PY3 = sys.version > '3'

if PY3:
    unicode = str
    long = int
    basestring = str

default_user_agent = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/57.0.2987.133 Safari/537.36"
)

logger = logging.getLogger('ghost')
logger.addHandler(logging.NullHandler())


class Error(Exception):
    """Base class for Ghost exceptions."""
    pass


class TimeoutError(Error):
    """Raised when a request times out"""
    pass


class QTMessageProxy(object):
    def __init__(self, logger):
        self.logger = logger

    def __call__(self, *args):
        msgType, msg = args[0], args[-1]
        levels = {
            QtDebugMsg: logging.DEBUG,
            QtWarningMsg: logging.WARNING,
            QtCriticalMsg: logging.CRITICAL,
            QtFatalMsg: logging.FATAL,
        }
        self.logger.log(levels[msgType], msg)


class GhostWebPage(QWebPage):
    """Overrides QtWebKit.QWebPage in order to intercept some graphical
    behaviours like alert(), confirm().
    Also intercepts client side console.log().
    """
    def __init__(self, session):
        self.session = session
        super(GhostWebPage, self).__init__()

    def chooseFile(self, frame, suggested_file=None):
        filename = self.session._upload_file
        self.session.logger.debug('Choosing file %s', filename)
        return filename

    def javaScriptConsoleMessage(self, message, line, source):
        """Prints client console message in current output stream."""
        super(GhostWebPage, self).javaScriptConsoleMessage(
            message,
            line,
            source,
        )
        self.session.logger.log(
            logging.WARNING if "Error" in message else logging.INFO,
            "%s(%d): %s", source or '<unknown>', line, message,
        )

    def javaScriptAlert(self, frame, message):
        """Notifies session for alert, then pass."""
        self.session._alert = message
        self.session.append_popup_message(message)
        self.session.logger.info("alert('%s')", message)

    def _get_value(self, value):
        return value() if callable(value) else value

    def javaScriptConfirm(self, frame, message):
        """Checks if session is waiting for confirm, then returns the right
        value.
        """
        if self.session._confirm_expected is None:
            raise Error(
                'You must specified a value to confirm "%s"' %
                message,
            )
        self.session.append_popup_message(message)
        value = self.session._confirm_expected
        self.session.logger.info("confirm('%s')", message)
        return self._get_value(value)

    def javaScriptPrompt(self, frame, message, defaultValue, result=None):
        """Checks if ghost is waiting for prompt, then enters the right
        value.
        """
        if self.session._prompt_expected is None:
            raise Error(
                'You must specified a value for prompt "%s"' %
                message,
            )
        self.session.append_popup_message(message)
        value = self.session._prompt_expected
        self.session.logger.info("prompt('%s')", message)
        value = self._get_value(value)

        # PySide and PyQt4 (on python3) and PyQt5 return a (bool, string)
        # 2-tuple
        # In some instance (like in unittest), value is not a string so set
        # a realistic replacement value
        #
        # FIXME: check if it makes sense to return false according to
        # self.session._prompt_expected
        if not isinstance(value, str):
            value = ''

        if value == '':
            self.session.logger.warning(
                "'%s' prompt filled with empty string", message,
            )

        if result is None:
            # PySide, PyQt4/PY3 and PyQt5 return branch
            return True, value

        result.append(unicode(value))
        return True

    def set_user_agent(self, user_agent):
        self.user_agent = user_agent

    def userAgentForUrl(self, url):
        return self.user_agent


def can_load_page(func):
    """Decorator that specifies if user can expect page loading from
    this action. If expect_loading is set to True, ghost will wait
    for page_loaded event.
    """
    @wraps(func)
    def wrapper(self, *args, **kwargs):
        expect_loading = kwargs.pop('expect_loading', False)

        if expect_loading:
            self.loaded = False
            func(self, *args, **kwargs)
            return self.wait_for_page_loaded(
                timeout=kwargs.pop('timeout', None))
        return func(self, *args, **kwargs)
    return wrapper


def qt_type_to_python(obj, encoding='iso-8859-1'):
    """Cast Qt binding object to a python type.

    Qt bindings do not have a consistent way of representing data types,
    sometimes even changing behavior according to running python version.

    This function is an attempt to workaround this while keeping
    the amount of extra code in below classes limited. It should return bytes
    or properly encoded string in intended usage scenario.

    :param obj: Qt object to cast (most likely a QByteArray)
    :param encoding: encoding to encoding `obj`'s data with.
    """
    data = obj.data()

    if encoding is None or isinstance(data, str):
        return data

    return data.decode(encoding)


class HttpResource(object):
    """Represents an HTTP resource.
    """
    def __init__(self, session, reply, content):
        self.session = session
        self.url = unicode(reply.url().toString())
        self.headers = {
            qt_type_to_python(header):
            qt_type_to_python(reply.rawHeader(header))
            for header in reply.rawHeaderList()
        }

        content_type = self.headers.get('Content-Type',
                                        'application/octet-stream')

        if content_type.startswith('text/'):
            charset = re.search(r'charset=([^;]+)', content_type)
            # As specified in RFC 2616 Section 3.7.1
            charset = charset.expand(r'\1') if charset else 'iso-8859-1'
            try:
                self.content = qt_type_to_python(content,
                                                 encoding=charset)
            except UnicodeDecodeError:
                # Server signaled text content but for some reason sent
                # non-text content. Reset Content-Type header.
                self.content = content.data()
                self.headers['Content-Type'] = 'application/octet-stream'
        else:
            self.content = content.data()

        self.http_status = reply.attribute(
            QNetworkRequest.HttpStatusCodeAttribute)
        self.session.logger.info(
            "Resource loaded: %s %s", self.url, self.http_status
        )

        self._reply = reply


def reply_ready_peek(reply):
    """Copy available bytes to `reply` data attribute.

    .. note:: Does not consume the `reply` buffer!

    :param reply: QNetworkReply object.
    """
    if not hasattr(reply, 'data'):
        reply.data = b'' if PY3 else ''

    reply.data += reply.peek(reply.bytesAvailable())


def reply_ready_read(reply):
    """Consume data from `reply` buffer.

    :param reply: QNetworkReply object.
    """
    reply.readAll()


def reply_destroyed(reply):
    """Handle `reply` destroyed signal.

    Hack required to avoid blocking on replies that for some reason never send
    the finished or error signal.

    :param reply: QNetworkReply object.
    """
    key = id(reply)
    try:
        qnam = reply.manager()
    except (RuntimeError, AttributeError):
        # reply is already destroyed
        reply_logger = logging.getLogger('ghost.reply.destroyed')
        reply_logger.debug('destroyed: %s', key)
        return

    # In some instance PySide and PyQt4 appear to attach the original
    # QNetworkAccessManager instead of our custom class (most likely because
    # the manager was destroyed already)
    if not isinstance(qnam, NetworkAccessManager):
        return

    reply_logger = qnam.logger
    reply_logger.debug('Reply for %s destroyed', reply.url().toString())

    if key in qnam._registry:
        qnam._registry.pop(key)
        reply_logger.warning(
            'Reply for %s did not trigger finished or error signal',
            reply.url().toString()
        )


def reply_download_progress(reply, received, total):
    """Log `reply` download progress."""
    try:
        reply_logger = reply.manager().logger
        reply_logger.debug('Downloading content of %s: %s of %s',
                           reply.url().toString(), received, total)
    except (RuntimeError, AttributeError):
        reply_logger = logging.getLogger('ghost.reply.downloadProgress')
        reply_logger.debug('Downloading content of reply %s: %s of %s',
                           id(reply), received, total)


def _reply_error_callback(reply, error_code):
    """Log an error message on QtNetworkReply error."""
    try:
        reply_logger = reply.manager().logger
        reply_logger.error('Reply for %s encountered an error: %s',
                           reply.url().toString(), reply.errorString())
    except (RuntimeError, AttributeError):
        reply_logger = logging.getLogger('ghost.reply.error')
        reply_logger.error('Reply for reply %s encountered an error: %s',
                           id(reply), error_code)


class NetworkAccessManager(QNetworkAccessManager):
    """Subclass QNetworkAccessManager to always cache the reply content

    :param exclude_regex: A regex use to determine wich url exclude
        when sending a request
    """
    def __init__(self, exclude_regex=None, logger=None, *args, **kwargs):
        self._regex = re.compile(exclude_regex) if exclude_regex else None
        self.logger = logger or logging.getLogger()
        super(NetworkAccessManager, self).__init__(*args, **kwargs)

        # Keep a registry of in-flight requests
        self._registry = {}
        self.finished.connect(self._reply_finished_callback)

    def createRequest(self, operation, request, data):
        """Create a new QNetworkReply."""
        if self._regex and self._regex.findall(
                unicode(request.url().toString())):
            reply = super(NetworkAccessManager, self).createRequest(
                QNetworkAccessManager.GetOperation,
                QNetworkRequest(QUrl())
            )
            self._registry[id(reply)] = reply
            return reply

        reply = super(NetworkAccessManager, self).createRequest(
            operation,
            request,
            data
        )
        reply.readyRead.connect(partial(reply_ready_peek, reply))
        reply.destroyed.connect(partial(reply_destroyed, reply))
        reply.downloadProgress.connect(partial(reply_download_progress, reply))
        reply.error.connect(partial(_reply_error_callback, reply))

        self.logger.debug('Registring reply %s for %s',
                          id(reply), reply.url().toString())
        self._registry[id(reply)] = reply
        return reply

    def _reply_finished_callback(self, reply):
        """Unregister a complete QNetworkReply."""
        self.logger.debug('Reply for %s complete', reply.url().toString())
        try:
            self._registry.pop(id(reply))
        except KeyError:
            # Workaround for QtWebkit bug #82506
            # https://bugs.webkit.org/show_bug.cgi?format=multiple&id=82506
            self.logger.debug('Reply was not in registry,'
                              'maybe webkit bug #82506')

    @property
    def requests(self):
        """Count in-flight QNetworkReply."""
        return len(self._registry)

    def __del__(self):
        self.logger.debug('Deleting QNetworkAccessManager %s', id(self))
        for _, reply in self._registry.items():
            try:
                self.logger.debug('Aborting %s', reply.url().toString())
                reply.abort()
                reply.deleteLater()
            except RuntimeError:
                # reply could be deleted already because QApplication stopped
                # before Python triggers QNAM.__del__, like on TimeoutError
                self.logger.debug('Reply for reply %s already deleted',
                                  id(reply))
                pass


class Ghost(object):
    """`Ghost` manages a Qt application.

    :param log_level: The optional logging level.
    :param log_handler: The optional logging handler.
    :param plugin_path: Array with paths to plugin directories
        (default ['/usr/lib/mozilla/plugins'])
    :param defaults: The defaults arguments to pass to new child sessions.
    """
    _app = None

    def __init__(
        self,
        plugin_path=['/usr/lib/mozilla/plugins', ],
        defaults=None,
    ):
        if not BINDING:
            raise RuntimeError("Ghost.py requires PySide, PyQt4 or PyQt5")

        qt_platform = os.environ.get('QT_QPA_PLATFORM', 'xcb')
        self.logger = logger.getChild('application')
        self.logger.info('Using QT_QPA_PLATFORM=%s', qt_platform)

        if qt_platform == 'xcb':
            if (
                sys.platform.startswith('linux') and
                'DISPLAY' not in os.environ
            ):
                try:
                    self.logger.debug('Using Xvfb display server')
                    self.xvfb = Xvfb(
                        width=800,
                        height=600,
                    )
                    self.xvfb.start()

                except OSError:
                    raise Error('Xvfb is required to a ghost run outside '
                                'an X instance')
            else:
                self.logger.debug('Using X11 display server %s',
                                  os.environ['DISPLAY'])

        # !!! Qt configuration for non X11 case is left to module consumers

        qInstallMsgHandler(QTMessageProxy(logging.getLogger('qt')))
        if plugin_path:
            for p in plugin_path:
                self.app.addLibraryPath(p)

        self.defaults = defaults or dict()

    @property
    def app(self):
        if Ghost._app is None:
            self.logger.info('Initializing QT application')
            Ghost._app = QApplication.instance() or QApplication(['ghost'])
        return Ghost._app

    def exit(self):
        self.logger.info('Stopping QT application')
        self.app.quit()
        if hasattr(self, 'xvfb'):
            self.logger.debug('Terminating Xvfb display server')
            self.xvfb.stop()

    def start(self, **kwargs):
        """Starts a new `Session`."""
        kwargs.update(self.defaults)
        return Session(self, **kwargs)

    def __del__(self):
        self.exit()


class Session(object):
    """`Session` manages a QWebPage.

    :param ghost: The parent `Ghost` instance.
    :param user_agent: The default User-Agent header.
    :param wait_timeout: Maximum step duration in second.
    :param wait_callback: An optional callable that is periodically
        executed until Ghost stops waiting.
    :param log_level: The optional logging level.
    :param log_handler: The optional logging handler.
    :param display: A boolean that tells ghost to displays UI.
    :param viewport_size: A tuple that sets initial viewport size.
    :param ignore_ssl_errors: A boolean that forces ignore ssl errors.
    :param cache_dir: A 2-tuple containing the path where to store cache data
      and its maximum size in bytes. If None, will default to
      $XDG_CACHE_HOME directory and either GHOST_CACHE_SIZE environment
      variable (in MB) or 50MB.
    :param plugins_enabled: Enable plugins (like Flash).
    :param java_enabled: Enable Java JRE.
    :param download_images: Indicate if the browser should download images
    :param exclude: A regex use to determine which url exclude
        when sending a request
    :param local_storage_enabled: An optional boolean to enable / disable
        local storage.
    """
    _alert = None
    _confirm_expected = None
    _prompt_expected = None
    _upload_file = None

    def __init__(
        self,
        ghost,
        user_agent=default_user_agent,
        wait_timeout=8,
        wait_callback=None,
        display=False,
        viewport_size=(800, 600),
        ignore_ssl_errors=True,
        cache_dir=None,
        plugins_enabled=False,
        java_enabled=False,
        javascript_enabled=True,
        download_images=True,
        show_scrollbars=True,
        exclude=None,
        network_access_manager_class=NetworkAccessManager,
        web_page_class=GhostWebPage,
        local_storage_enabled=True,
    ):
        self.ghost = ghost

        self.id = str(uuid.uuid4())

        self.logger = logging.LoggerAdapter(
            logger.getChild('session'),
            {'session': self.id},
        )
        self.logger.info("Starting new session")

        self.http_resources = []

        self.wait_timeout = wait_timeout
        self.wait_callback = wait_callback
        self.ignore_ssl_errors = ignore_ssl_errors
        self.loaded = True

        self.display = display

        self.popup_messages = []
        self.page = web_page_class(self)

        if network_access_manager_class is not None:
            self.page.setNetworkAccessManager(
                network_access_manager_class(exclude_regex=exclude,
                                             logger=self.logger))

        # Network disk cache
        cache = QNetworkDiskCache(self.ghost.app)
        if cache_dir:
            cache.setCacheDirectory(cache_dir[0])
            cache.setMaximumCacheSize(cache_dir[1])
        else:
            cache.setCacheDirectory(
                os.environ.get('XDG_CACHE_HOME',
                               os.path.expanduser("~/.cache/ghost-py")))
            cache.setMaximumCacheSize(
                int(os.environ.get('GHOST_CACHE_SIZE', 50)) * 1024 * 1024
            )
        self.page.networkAccessManager().setCache(cache)

        QtWebKit.QWebSettings.setMaximumPagesInCache(0)
        QtWebKit.QWebSettings.setObjectCacheCapacities(0, 0, 0)
        QtWebKit.QWebSettings.globalSettings().setAttribute(
            QtWebKit.QWebSettings.LocalStorageEnabled, local_storage_enabled)

        self.page.setForwardUnsupportedContent(True)
        self.page.settings().setAttribute(
            QtWebKit.QWebSettings.AutoLoadImages, download_images)
        self.page.settings().setAttribute(
            QtWebKit.QWebSettings.PluginsEnabled, plugins_enabled)
        self.page.settings().setAttribute(
            QtWebKit.QWebSettings.JavaEnabled,
            java_enabled,
        )
        self.page.settings().setAttribute(
            QtWebKit.QWebSettings.JavascriptEnabled, javascript_enabled)

        if not show_scrollbars:
            self.page.mainFrame().setScrollBarPolicy(
                QtCore.Qt.Vertical,
                QtCore.Qt.ScrollBarAlwaysOff,
            )
            self.page.mainFrame().setScrollBarPolicy(
                QtCore.Qt.Horizontal,
                QtCore.Qt.ScrollBarAlwaysOff,
            )

        self.set_viewport_size(*viewport_size)

        # Page signals
        self.page.loadFinished.connect(self._page_loaded)
        self.page.loadStarted.connect(self._page_load_started)
        self.page.unsupportedContent.connect(self._unsupported_content)

        self.manager = self.page.networkAccessManager()
        self.manager.finished.connect(self._request_ended)
        self.manager.sslErrors.connect(self._on_manager_ssl_errors)

        # Cookie jar
        self.cookie_jar = QNetworkCookieJar()
        self.manager.setCookieJar(self.cookie_jar)

        # User Agent
        self.page.set_user_agent(user_agent)

        self.page.networkAccessManager().authenticationRequired\
            .connect(self._authenticate)
        self.page.networkAccessManager().proxyAuthenticationRequired\
            .connect(self._authenticate)

        self.main_frame = self.page.mainFrame()

        class GhostQWebView(QWebView):
            def sizeHint(self):
                return QSize(*viewport_size)

        self.webview = GhostQWebView()

        if plugins_enabled:
            self.webview.settings().setAttribute(
                QtWebKit.QWebSettings.PluginsEnabled, True)
        if java_enabled:
            self.webview.settings().setAttribute(
                QtWebKit.QWebSettings.JavaEnabled, True)

        self.webview.setPage(self.page)

        if self.display:
            self.show()

    def frame(self, selector=None):
        """ Set main frame as current main frame's parent.

        :param frame: An optional name or index of the child to descend to.
        """
        if isinstance(selector, basestring):
            for frame in self.main_frame.childFrames():
                if frame.frameName() == selector:
                    self.main_frame = frame
                    return
            # frame not found so we throw an exception
            raise LookupError(
                "Child frame for name '%s' not found." % selector,
            )

        if isinstance(selector, int):
            try:
                self.main_frame = self.main_frame.childFrames()[selector]
                return
            except IndexError:
                raise LookupError(
                    "Child frame at index '%s' not found." % selector,
                )

        # we can't ascend directly to parent frame because it might have been
        # deleted
        self.main_frame = self.page.mainFrame()

    @can_load_page
    def call(self, selector, method):
        """Call method on element matching given selector.

        :param selector: A CSS selector to the target element.
        :param method: The name of the method to call.
        :param expect_loading: Specifies if a page loading is expected.
        """
        self.logger.debug('Calling `%s` method on `%s`', method, selector)
        element = self.main_frame.findFirstElement(selector)
        return element.evaluateJavaScript('this[%s]();' % repr(method))

    def capture(
        self,
        region=None,
        selector=None,
        format=None,
    ):
        """Returns snapshot as QImage.

        :param region: An optional tuple containing region as pixel
            coodinates.
        :param selector: A selector targeted the element to crop on.
        :param format: The output image format.
        """

        if format is None:
            format = QImage.Format_ARGB32_Premultiplied

        self.main_frame.setScrollBarPolicy(
            QtCore.Qt.Vertical,
            QtCore.Qt.ScrollBarAlwaysOff,
        )
        self.main_frame.setScrollBarPolicy(
            QtCore.Qt.Horizontal,
            QtCore.Qt.ScrollBarAlwaysOff,
        )
        frame_size = self.main_frame.contentsSize()
        max_size = 23170 * 23170
        if frame_size.height() * frame_size.width() > max_size:
            self.logger.warning("Frame size is too large.")
            default_size = self.page.viewportSize()
            if default_size.height() * default_size.width() > max_size:
                return None
        else:
            self.page.setViewportSize(self.main_frame.contentsSize())

        self.logger.info("Frame size -> %s", str(self.page.viewportSize()))

        image = QImage(self.page.viewportSize(), format)
        painter = QPainter(image)

        if region is None and selector is not None:
            region = self.region_for_selector(selector)

        if region:
            x1, y1, x2, y2 = region
            w, h = (x2 - x1), (y2 - y1)
            reg = QRegion(x1, y1, w, h)
            self.main_frame.render(painter, reg)
        else:
            self.main_frame.render(painter)

        painter.end()

        if region:
            x1, y1, x2, y2 = region
            w, h = (x2 - x1), (y2 - y1)
            image = image.copy(x1, y1, w, h)

        return image

    def capture_to(
        self,
        path,
        region=None,
        selector=None,
        format=None,
    ):
        """Saves snapshot as image.

        :param path: The destination path.
        :param region: An optional tuple containing region as pixel
            coodinates.
        :param selector: A selector targeted the element to crop on.
        :param format: The output image format.
        """

        if format is None:
            format = QImage.Format_ARGB32_Premultiplied

        self.capture(region=region, format=format,
                     selector=selector).save(path)

    def print_to_pdf(
        self,
        path,
        paper_size=(8.5, 11.0),
        paper_margins=(0, 0, 0, 0),
        paper_units=None,
        zoom_factor=1.0,
    ):
        """Saves page as a pdf file.

        See qt4 QPrinter documentation for more detailed explanations
        of options.

        :param path: The destination path.
        :param paper_size: A 2-tuple indicating size of page to print to.
        :param paper_margins: A 4-tuple indicating size of each margin.
        :param paper_units: Units for pager_size, pager_margins.
        :param zoom_factor: Scale the output content.
        """
        assert len(paper_size) == 2
        assert len(paper_margins) == 4

        if paper_units is None:
            paper_units = QPrinter.Inch

        printer = QPrinter(mode=QPrinter.ScreenResolution)
        printer.setOutputFormat(QPrinter.PdfFormat)
        printer.setPaperSize(QtCore.QSizeF(*paper_size), paper_units)
        printer.setPageMargins(*(paper_margins + (paper_units,)))
        if paper_margins != (0, 0, 0, 0):
            printer.setFullPage(True)
        printer.setOutputFileName(path)
        if self.webview is None:
            self.webview = QWebView()
            self.webview.setPage(self.page)
        self.webview.setZoomFactor(zoom_factor)
        self.webview.print_(printer)

    @can_load_page
    def click(self, selector, btn=0):
        """Click the targeted element.

        :param selector: A CSS3 selector to targeted element.
        :param btn: The number of mouse button.
            0 - left button,
            1 - middle button,
            2 - right button
        """
        if not self.exists(selector):
            raise Error("Can't find element to click")
        return self.evaluate("""
            (function () {
                var element = document.querySelector(%s);
                var evt = document.createEvent("MouseEvents");
                evt.initMouseEvent("click", true, true, window, 1, 1, 1, 1, 1,
                    false, false, false, false, %s, element);
                return element.dispatchEvent(evt);
            })();
        """ % (repr(selector), str(btn)))

    @contextmanager
    def confirm(self, confirm=True):
        """Statement that tells Ghost how to deal with javascript confirm().

        :param confirm: A boolean or a callable to set confirmation.
        """
        self._confirm_expected = confirm
        yield
        self._confirm_expected = None

    @property
    def content(self, to_unicode=True):
        """Returns current frame HTML as a string.

        :param to_unicode: Whether to convert html to unicode or not
        """
        if to_unicode:
            return unicode(self.main_frame.toHtml())
        else:
            return self.main_frame.toHtml()

    @property
    def cookies(self):
        """Returns all cookies."""
        return self.cookie_jar.allCookies()

    def delete_cookies(self):
        """Deletes all cookies."""
        self.cookie_jar.setAllCookies([])

    def clear_alert_message(self):
        """Clears the alert message"""
        self._alert = None

    def clear_cache(self):
        """Clear disk cache."""
        cache = self.manager.cache()
        if cache:
            cache.clear()

    @can_load_page
    def evaluate(self, script):
        """Evaluates script in page frame.

        :param script: The script to evaluate.
        """
        return (
            self.main_frame.evaluateJavaScript("%s" % script),
            self._release_last_resources(),
        )

    def evaluate_js_file(self, path, encoding='utf-8', **kwargs):
        """Evaluates javascript file at given path in current frame.
        Raises native IOException in case of invalid file.

        :param path: The path of the file.
        :param encoding: The file's encoding.
        """
        with codecs.open(path, encoding=encoding) as f:
            return self.evaluate(f.read(), **kwargs)

    def exists(self, selector):
        """Checks if element exists for given selector.

        :param string: The element selector.
        """
        return not self.main_frame.findFirstElement(selector).isNull()

    def exit(self):
        """Exits all Qt widgets."""
        self.logger.info("Closing session")
        self.page.deleteLater()
        self.webview.deleteLater()
        self.cookie_jar.deleteLater()
        self.manager.deleteLater()
        self.main_frame.deleteLater()

    @can_load_page
    def fill(self, selector, values):
        """Fills a form with provided values.

        :param selector: A CSS selector to the target form to fill.
        :param values: A dict containing the values.
        """
        if not self.exists(selector):
            raise Error("Can't find form")
        resources = []
        for field in values:
            r, res = self.set_field_value(
                "%s [name=%s]" % (selector, repr(field)), values[field])
            resources.extend(res)
        return True, resources

    @can_load_page
    def fire(self, selector, event):
        """Fire `event` on element at `selector`

        :param selector: A selector to target the element.
        :param event: The name of the event to trigger.
        """
        self.logger.debug('Fire `%s` on `%s`', event, selector)
        element = self.main_frame.findFirstElement(selector)
        return element.evaluateJavaScript("""
            var event = document.createEvent("HTMLEvents");
            event.initEvent('%s', true, true);
            this.dispatchEvent(event);
        """ % event)

    def global_exists(self, global_name):
        """Checks if javascript global exists.

        :param global_name: The name of the global.
        """
        return self.evaluate(
            '!(typeof this[%s] === "undefined");'
            % repr(global_name)
        )[0]

    def hide(self):
        """Close the webview."""
        try:
            self.webview.close()
        except:
            raise Error("no webview to close")

    def load_cookies(self, cookie_storage, keep_old=False):
        """load from cookielib's CookieJar or Set-Cookie3 format text file.

        :param cookie_storage: file location string on disk or CookieJar
            instance.
        :param keep_old: Don't reset, keep cookies not overridden.
        """
        def toQtCookieJar(PyCookieJar, QtCookieJar):
            allCookies = QtCookieJar.allCookies() if keep_old else []
            for pc in PyCookieJar:
                qc = toQtCookie(pc)
                allCookies.append(qc)
            QtCookieJar.setAllCookies(allCookies)

        def toQtCookie(PyCookie):
            qc = QNetworkCookie(
                PyCookie.name.encode('utf-8'),
                PyCookie.value.encode('utf-8')
            )
            qc.setSecure(PyCookie.secure)
            if PyCookie.path_specified:
                qc.setPath(PyCookie.path)
            if PyCookie.domain != "":
                qc.setDomain(PyCookie.domain)
            if PyCookie.expires and PyCookie.expires != 0:
                t = QDateTime()
                t.setTime_t(PyCookie.expires)
                qc.setExpirationDate(t)
            # not yet handled(maybe less useful):
            #   py cookie.rest / QNetworkCookie.setHttpOnly()
            return qc

        if isinstance(cookie_storage, str):
            cj = LWPCookieJar(cookie_storage)
            cj.load()
            toQtCookieJar(cj, self.cookie_jar)
        elif isinstance(cookie_storage, CookieJar):
            toQtCookieJar(cookie_storage, self.cookie_jar)
        else:
            raise ValueError('unsupported cookie_storage type.')

    def open(
        self,
        address,
        method='get',
        headers={},
        auth=None,
        body=None,
        default_popup_response=None,
        wait=True,
        timeout=None,
        client_certificate=None,
        encode_url=True,
        user_agent=None,
        use_cache=True,
    ):
        """Opens a web page.

        :param address: The resource URL.
        :param method: The Http method.
        :param headers: An optional dict of extra request headers.
        :param auth: An optional tuple of HTTP auth (username, password).
        :param body: An optional string containing a payload.
        :param default_popup_response: the default response for any confirm/
        alert/prompt popup from the Javascript (replaces the need for the with
        blocks)
        :param wait: If set to True (which is the default), this
        method call waits for the page load to complete before
        returning.  Otherwise, it just starts the page load task and
        it is the caller's responsibilty to wait for the load to
        finish by other means (e.g. by calling wait_for_page_loaded()).
        :param timeout: An optional timeout.
        :param client_certificate An optional dict with "certificate_path" and
        "key_path" both paths corresponding to the certificate and key files
        :param encode_url Set to true if the url have to be encoded
        :param user_agent An option user agent string.
        :param use_cache: Whether to use disk cache.
        :return: Page resource, and all loaded resources, unless wait
        is False, in which case it returns None.
        """
        self.logger.info('Opening %s', address)
        body = body or QByteArray()
        try:
            method = getattr(QNetworkAccessManager,
                             "%sOperation" % method.capitalize())
        except AttributeError:
            raise Error("Invalid http method %s" % method)

        if user_agent is not None:
            self.page.set_user_agent(user_agent)

        if client_certificate:
            ssl_conf = QSslConfiguration.defaultConfiguration()

            if "certificate_path" in client_certificate:
                try:
                    certificate = QtNetwork.QSslCertificate.fromPath(
                        client_certificate["certificate_path"],
                        QSsl.Pem,
                    )[0]
                except IndexError:
                    raise Error(
                        "Can't find certicate in %s"
                        % client_certificate["certificate_path"]
                    )

                ssl_conf.setLocalCertificate(certificate)

            if "key_path" in client_certificate:
                private_key = QtNetwork.QSslKey(
                    open(client_certificate["key_path"]).read(),
                    QSsl.Rsa,
                )
                ssl_conf.setPrivateKey(private_key)

            QSslConfiguration.setDefaultConfiguration(ssl_conf)

        if encode_url:
            request = QNetworkRequest(QUrl(address))
        else:
            request = QNetworkRequest(QUrl.fromEncoded(address))

        if use_cache and self.manager.cache() is not None:
            self.logger.debug('Using disk cache')
            request.CacheLoadControl(1)
        else:
            self.logger.debug('Not using disk cache')
            request.CacheLoadControl(0)

        for header in headers:
            request.setRawHeader(header, headers[header])

        self._auth = auth
        self._auth_attempt = 0  # Avoids reccursion

        self.main_frame.load(request, method, body)
        self.loaded = False

        if default_popup_response is not None:
            self._prompt_expected = default_popup_response
            self._confirm_expected = default_popup_response

        if wait:
            return self.wait_for_page_loaded(timeout=timeout)

    def scroll_to_anchor(self, anchor):
        self.main_frame.scrollToAnchor(anchor)

    @contextmanager
    def prompt(self, value=''):
        """Statement that tells Ghost how to deal with javascript prompt().

        :param value: A string or a callable value to fill in prompt.
        """
        self._prompt_expected = value
        yield
        self._prompt_expected = None

    def region_for_selector(self, selector):
        """Returns frame region for given selector as tuple.

        :param selector: The targeted element.
        """
        geo = self.main_frame.findFirstElement(selector).geometry()
        try:
            region = (geo.left(), geo.top(), geo.right(), geo.bottom())
        except:
            raise Error("can't get region for selector '%s'" % selector)
        return region

    def save_cookies(self, cookie_storage):
        """Save to cookielib's CookieJar or Set-Cookie3 format text file.

        :param cookie_storage: file location string or CookieJar instance.
        """
        def toPyCookieJar(QtCookieJar, PyCookieJar):
            for c in QtCookieJar.allCookies():
                PyCookieJar.set_cookie(toPyCookie(c))

        def toPyCookie(QtCookie):
            port = None
            port_specified = False
            secure = QtCookie.isSecure()
            name = qt_type_to_python(QtCookie.name())
            value = qt_type_to_python(QtCookie.value())
            v = str(QtCookie.path())
            path_specified = bool(v != "")
            path = v if path_specified else None
            v = str(QtCookie.domain())
            domain_specified = bool(v != "")
            domain = v
            if domain_specified:
                domain_initial_dot = v.startswith('.')
            else:
                domain_initial_dot = None
            v = long(QtCookie.expirationDate().toTime_t())
            # Long type boundary on 32bit platfroms; avoid ValueError
            expires = 2147483647 if v > 2147483647 else v
            rest = {}
            discard = False
            return Cookie(
                0,
                name,
                value,
                port,
                port_specified,
                domain,
                domain_specified,
                domain_initial_dot,
                path,
                path_specified,
                secure,
                expires,
                discard,
                None,
                None,
                rest,
            )

        if isinstance(cookie_storage, str):
            cj = LWPCookieJar(cookie_storage)
            toPyCookieJar(self.cookie_jar, cj)
            cj.save()
        elif isinstance(cookie_storage, CookieJar):
            toPyCookieJar(self.cookie_jar, cookie_storage)
        else:
            raise ValueError('unsupported cookie_storage type.')

    @can_load_page
    def set_field_value(self, selector, value, blur=True):
        """Sets the value of the field matched by given selector.

        :param selector: A CSS selector that target the field.
        :param value: The value to fill in.
        :param blur: An optional boolean that force blur when filled in.
        """
        self.logger.debug('Setting value "%s" for "%s"', value, selector)

        def _set_checkbox_value(el, value):
            el.setFocus()
            if value is True:
                el.setAttribute('checked', 'checked')
            else:
                el.removeAttribute('checked')

        def _set_checkboxes_value(els, value):
            for el in els:
                if el.attribute('value') == value:
                    _set_checkbox_value(el, True)
                else:
                    _set_checkbox_value(el, False)

        def _set_radio_value(els, value):
            for el in els:
                if el.attribute('value') == value:
                    el.setFocus()
                    el.setAttribute('checked', 'checked')

        def _set_text_value(el, value):
            el.setFocus()
            el.setAttribute('value', value)

        def _set_select_value(el, value):
            el.setFocus()
            index = 0
            for option in el.findAll('option'):
                if option.attribute('value') == value:
                    option.evaluateJavaScript('this.selected = true;')
                    el.evaluateJavaScript('this.selectedIndex = %d;' % index)
                    break
                index += 1

        def _set_textarea_value(el, value):
            el.setFocus()
            el.setPlainText(value)

        res, resources = None, []
        element = self.main_frame.findFirstElement(selector)
        if element.isNull():
            raise Error('can\'t find element for %s"' % selector)

        tag_name = str(element.tagName()).lower()

        if tag_name == "select":
            _set_select_value(element, value)
        elif tag_name == "textarea":
            _set_textarea_value(element, value)
        elif tag_name == "input":
            type_ = str(element.attribute('type')).lower()
            if type_ in [
                "color",
                "date",
                "datetime",
                "datetime-local",
                "email",
                "hidden",
                "month",
                "number",
                "password",
                "range",
                "search",
                "tel",
                "text",
                "time",
                "url",
                "week",
                "",
            ]:
                _set_text_value(element, value)
            elif type_ == "checkbox":
                els = self.main_frame.findAllElements(selector)
                if els.count() > 1:
                    _set_checkboxes_value(els, value)
                else:
                    _set_checkbox_value(element, value)
            elif type_ == "radio":
                _set_radio_value(
                    self.main_frame.findAllElements(selector),
                    value,
                )
            elif type_ == "file":
                self._upload_file = value
                res, resources = self.click(selector)

                self._upload_file = None
        else:
            raise Error('unsupported field tag')

        for event in ['input', 'change']:
            self.fire(selector, event)

        if blur:
            self.call(selector, 'blur')

        return res, resources

    def set_proxy(
        self,
        type_,
        host='localhost',
        port=8888,
        user='',
        password='',
    ):
        """Set up proxy for FURTHER connections.

        :param type_: proxy type to use: \
            none/default/socks5/https/http.
        :param host: proxy server ip or host name.
        :param port: proxy port.
        """
        _types = {
            'default': QNetworkProxy.DefaultProxy,
            'none': QNetworkProxy.NoProxy,
            'socks5': QNetworkProxy.Socks5Proxy,
            'https': QNetworkProxy.HttpProxy,
            'http': QNetworkProxy.HttpCachingProxy
        }

        if type_ is None:
            type_ = 'none'
        type_ = type_.lower()
        if type_ in ['none', 'default']:
            self.manager.setProxy(QNetworkProxy(_types[type_]))
            return
        elif type_ in _types:
            proxy = QNetworkProxy(
                _types[type_],
                hostName=host,
                port=port,
                user=user,
                password=password,
            )
            self.manager.setProxy(proxy)
        else:
            raise ValueError(
                'Unsupported proxy type: %s' % type_ +
                '\nsupported types are: none/socks5/http/https/default',
            )

    def set_viewport_size(self, width, height):
        """Sets the page viewport size.

        :param width: An integer that sets width pixel count.
        :param height: An integer that sets height pixel count.
        """
        self.page.setViewportSize(QSize(width, height))

    def append_popup_message(self, message):
        self.popup_messages.append(unicode(message))

    def show(self):
        """Show current page inside a QWebView.
        """
        self.logger.debug('Showing webview')
        self.webview.show()
        self.ghost.app.processEvents()

    def sleep(self, value=0.1):
        started_at = time.time()

        while time.time() <= (started_at + value):
            time.sleep(value / 10)
            self.ghost.app.processEvents()

    def wait_for(self, condition, timeout_message, timeout=None):
        """Waits until condition is True.

        :param condition: A callable that returns the condition.
        :param timeout_message: The exception message on timeout.
        :param timeout: An optional timeout.
        """
        timeout = self.wait_timeout if timeout is None else timeout
        started_at = time.time()
        while not condition():
            if time.time() > (started_at + timeout):
                self.logger.debug('Timeout with %d requests still in flight',
                                  self.manager.requests)
                raise TimeoutError(timeout_message)
            self.sleep(value=timeout / 10)
            if self.wait_callback is not None:
                self.wait_callback()

    def wait_for_alert(self, timeout=None):
        """Waits for main frame alert().

        :param timeout: An optional timeout.
        """
        self.wait_for(lambda: self._alert is not None,
                      'User has not been alerted.', timeout)
        msg = self._alert
        self._alert = None
        return msg, self._release_last_resources()

    def wait_for_page_loaded(self, timeout=None):
        """Waits until page is loaded, assumed that a page as been requested.

        :param timeout: An optional timeout.
        """
        self.wait_for(lambda: self.loaded and self.manager.requests == 0,
                      'Unable to load requested page', timeout)
        resources = self._release_last_resources()
        page = None

        url = self.main_frame.url().toString()
        url_without_hash = url.split("#")[0]

        for resource in resources:
            if url == resource.url or url_without_hash == resource.url:
                page = resource

        self.logger.info('Page loaded %s', url)

        return page, resources

    def wait_for_selector(self, selector, timeout=None):
        """Waits until selector match an element on the frame.

        :param selector: The selector to wait for.
        :param timeout: An optional timeout.
        """
        self.wait_for(
            lambda: self.exists(selector),
            'Can\'t find element matching "%s"' % selector,
            timeout,
        )
        return True, self._release_last_resources()

    def wait_while_selector(self, selector, timeout=None):
        """Waits until the selector no longer matches an element on the frame.

        :param selector: The selector to wait for.
        :param timeout: An optional timeout.
        """
        self.wait_for(
            lambda: not self.exists(selector),
            'Element matching "%s" is still available' % selector,
            timeout,
        )
        return True, self._release_last_resources()

    def wait_for_text(self, text, timeout=None):
        """Waits until given text appear on main frame.

        :param text: The text to wait for.
        :param timeout: An optional timeout.
        """
        self.wait_for(
            lambda: text in self.content,
            'Can\'t find "%s" in current frame' % text,
            timeout,
        )
        return True, self._release_last_resources()

    def _authenticate(self, mix, authenticator):
        """Called back on basic / proxy http auth.

        :param mix: The QNetworkReply or QNetworkProxy object.
        :param authenticator: The QAuthenticator object.
        """
        if self._auth is not None and self._auth_attempt == 0:
            username, password = self._auth
            authenticator.setUser(username)
            authenticator.setPassword(password)
            self._auth_attempt += 1

    def _page_loaded(self):
        """Called back when page is loaded.
        """
        self.loaded = True

    def _page_load_started(self):
        """Called back when page load started.
        """
        self.loaded = False

    def _release_last_resources(self):
        """Releases last loaded resources.

        :return: The released resources.
        """
        last_resources = self.http_resources
        self.http_resources = []
        return last_resources

    def _request_ended(self, reply):
        """Adds an HttpResource object to http_resources.

        :param reply: The QNetworkReply object.
        """

        if reply.attribute(QNetworkRequest.HttpStatusCodeAttribute):
            self.logger.debug("[%s] bytesAvailable()= %s",
                              reply.url().toString(), reply.bytesAvailable())

            try:
                content = reply.data
            except AttributeError:
                content = reply.readAll()

            self.http_resources.append(HttpResource(
                self,
                reply,
                content=content,
            ))

    def _unsupported_content(self, reply):
        self.logger.info("Unsupported content %s", reply.url().toString())
        # reply went though reply_read_peek already, consume buffer to avoid
        # duplication on next "ready" signal handling and connect callback
        reply_ready_read(reply)
        reply.readyRead.connect(partial(reply_ready_read, reply))

    def _on_manager_ssl_errors(self, reply, errors):
        if self.ignore_ssl_errors:
            reply.ignoreSslErrors()
        else:
            self.logger.warning('SSL certificate error: %s',
                                reply.url().toString())

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.exit()
