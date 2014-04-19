import ConfigParser
import locale
import logging

import pycountry
import pyocr

from paperwork.backend.config import PaperworkConfig
from paperwork.backend.config import PaperworkSetting
from paperwork.backend.config import paperwork_cfg_boolean


logger = logging.getLogger(__name__)
DEFAULT_CALIBRATION_RESOLUTION = 200
DEFAULT_OCR_LANG = "eng"  # if really we can't guess anything
RECOMMENDED_SCAN_RESOLUTION = 300


class _ScanTimes(object):
    """
    Helper to find, load and rewrite the scan times stored in the configuration
    """
    __ITEM_2_CONFIG = {
        'calibration': ('Scanner', 'ScanTimeCalibration'),
        'normal': ('Scanner', 'ScanTime'),
        'ocr': ('OCR', 'OCRTime'),
    }

    def __init__(self):
        self.section = self.__ITEM_2_CONFIG['normal'][0]
        self.values = {}
        self.value = self

    def load(self, config):
        for (k, cfg) in self.__ITEM_2_CONFIG.iteritems():
            try:
                value = float(config.get(cfg[0], cfg[1]))
                self.values[k] = value
            except (ConfigParser.NoOptionError, ConfigParser.NoSectionError):
                if k in self.values:
                    self.values.pop(k)

    def update(self, config):
        for (k, v) in self.values.iteritems():
            if k not in self.__ITEM_2_CONFIG:
                logger.warning("Got timing for '%s' but don't know how to"
                               " store it" % k)
                continue
            cfg = self.__ITEM_2_CONFIG[k]
            config.set(cfg[0], cfg[1], str(v))

    def __getitem__(self, item):
        if item in self.values:
            return self.values[item]
        return 60.0

    def __setitem__(self, item, value):
        self.values[item] = value

    def __get_value(self):
        return self


class _PaperworkScannerCalibration(object):
    def __init__(self, section):
        self.section = section
        self.value = None

    def load(self, config):
        try:
            pt_a_x = int(config.get(
                "Scanner", "Calibration_Pt_A_X"))
            pt_a_y = int(config.get(
                "Scanner", "Calibration_Pt_A_Y"))
            pt_b_x = int(config.get(
                "Scanner", "Calibration_Pt_B_X"))
            pt_b_y = int(config.get(
                "Scanner", "Calibration_Pt_B_Y"))
            if (pt_a_x > pt_b_x):
                (pt_a_x, pt_b_x) = (pt_b_x, pt_a_x)
            if (pt_a_y > pt_b_y):
                (pt_a_y, pt_b_y) = (pt_b_y, pt_a_y)

            resolution = DEFAULT_CALIBRATION_RESOLUTION
            try:
                resolution = int(config.get(
                    "Scanner", "Calibration_Resolution"))
            except (ConfigParser.NoOptionError, ConfigParser.NoSectionError):
                logger.warning("Calibration resolution is not specified in the"
                               " configuration. Will assume the calibration was"
                               " done with a resolution of %ddpi" % resolution)

            self.value = (resolution, ((pt_a_x, pt_a_y), (pt_b_x, pt_b_y)))
        except (ConfigParser.NoOptionError, ConfigParser.NoSectionError):
            # no calibration -> no cropping -> we have to keep the whole image
            # each time
            self.value = None

    def update(self, config):
        if self.value is None:
            return
        config.set("Scanner", "Calibration_Resolution",
                   str(self.value[0]))
        config.set("Scanner", "Calibration_Pt_A_X",
                   str(self.value[1][0][0]))
        config.set("Scanner", "Calibration_Pt_A_Y",
                   str(self.value[1][0][1]))
        config.set("Scanner", "Calibration_Pt_B_X",
                   str(self.value[1][1][0]))
        config.set("Scanner", "Calibration_Pt_B_Y",
                   str(self.value[1][1][1]))


class _PaperworkCfgStringList(list):
    def __init__(self, string):
        elements = string.split(",")
        for element in elements:
            self.append(element)

    def __str__(self):
        return ",".join(self)


class _PaperworkLangs(object):
    """
    Convenience setting. Gives all the languages used as one dictionary
    """
    def __init__(self, ocr_lang_setting, spellcheck_lang_setting):
        self.ocr_lang_setting = ocr_lang_setting
        self.spellcheck_lang_setting = spellcheck_lang_setting
        self.section = "OCR"

    def __get_langs(self):
        ocr_lang = self.ocr_lang_setting.value
        if ocr_lang is None:
            return None
        return {
            'ocr': ocr_lang,
            'spelling': self.spellcheck_lang_setting.value
        }

    value = property(__get_langs)

    @staticmethod
    def load(_):
        pass

    @staticmethod
    def update(_):
        pass


class _PaperworkSize(object):
    def __init__(self, section, base_token,
                 default_size=(1024, 768),
                 min_size=(400, 300)):
        self.section = section
        self.base_token = base_token
        self.value = default_size
        self.default_size = default_size
        self.min_size = min_size

    def load(self, config):
        try:
            w = config.get(self.section, self.base_token + "_w")
            w = int(w)
            if w < self.min_size[0]:
                w = self.min_size[0]
            h = config.get(self.section, self.base_token + "_h")
            h = int(h)
            if h < self.min_size[1]:
                h = self.min_size[1]
            self.value = (w, h)
            return
        except (ConfigParser.NoOptionError, ConfigParser.NoSectionError):
            self.value = self.default_size

    def update(self, config):
        config.set(self.section, self.base_token + "_w", str(self.value[0]))
        config.set(self.section, self.base_token + "_h", str(self.value[1]))


class _PaperworkFrontendConfigUtil:
    @staticmethod
    def get_default_ocr_lang():
        # Try to guess based on the system locale what would be
        # the best OCR language

        ocr_tools = pyocr.get_available_tools()
        if (len(ocr_tools) < 0):
            return DEFAULT_OCR_LANG
        ocr_langs = ocr_tools[0].get_available_languages()

        default_locale_long = locale.getdefaultlocale()[0]
        # Usually something like "fr_FR" --> we just need the first part
        default_locale = default_locale_long.split("_")[0]
        try:
            lang = pycountry.pycountry.languages.get(alpha2=default_locale)
            for ocr_lang in (lang.terminology, lang.bibliographic):
                if ocr_lang in ocr_langs:
                    return ocr_lang
        except Exception, exc:
            logger.error("Warning: Failed to figure out system language"
                   " (locale is [%s]). Will default to %s"
                   % (default_locale_long, default_locale_long))
            logger.error('Exception was: %s' % exc)
        return DEFAULT_OCR_LANG

    @staticmethod
    def get_default_spellcheck_lang(ocr_lang):
        ocr_lang = ocr_lang.value
        if ocr_lang is None:
            return None

        # Try to guess the lang based on the ocr lang
        try:
            language = pycountry.languages.get(terminology=ocr_lang[:3])
        except KeyError:
            language = pycountry.languages.get(bibliographic=ocr_lang[:3])
        spelling_lang = language.alpha2
        return spelling_lang


def load_config():
    config = PaperworkConfig()

    settings = {
        'main_win_size' : _PaperworkSize("GUI", "main_win_size"),
        'ocr_enabled' : PaperworkSetting("OCR", "Enabled", lambda: True,
                                         paperwork_cfg_boolean),
        'ocr_lang' : PaperworkSetting("OCR", "Lang",
                                      _PaperworkFrontendConfigUtil.get_default_ocr_lang),
        'ocr_nb_angles' : PaperworkSetting("OCR", "Nb_Angles", lambda: 4, int),
        'result_sorting' : PaperworkSetting("GUI", "Sorting", lambda: "scan_date"),
        'scanner_calibration' : _PaperworkScannerCalibration("Scanner"),
        'scanner_devid' : PaperworkSetting("Scanner", "Device"),
        'scanner_resolution' : PaperworkSetting("Scanner", "Resolution",
                                                lambda: RECOMMENDED_SCAN_RESOLUTION,
                                                int),
        'scanner_source' : PaperworkSetting("Scanner", "Source"),
        'scanner_sources' : PaperworkSetting("Scanner", "Sources",
                                             lambda: _PaperworkCfgStringList(""),
                                             _PaperworkCfgStringList),
        'scanner_mode' : PaperworkSetting("Scanner", "Mode"),
        'scanner_modes' : PaperworkSetting("Scanner", "Modes",
                                           lambda: _PaperworkCfgStringList(""),
                                           _PaperworkCfgStringList),
        'scan_time' : _ScanTimes(),
        'zoom_level' : PaperworkSetting("GUI", "zoom_level", lambda: 0.0, float),
    }
    ocr_lang = _PaperworkFrontendConfigUtil.get_default_spellcheck_lang
    settings['spelling_lang'] = (
        PaperworkSetting("SpellChecking", "Lang",
                         lambda: ocr_lang(settings['ocr_lang']))
    )
    settings['langs'] = (
        _PaperworkLangs(settings['ocr_lang'], settings['spelling_lang'])
    )

    for (k, v) in settings.iteritems():
        config.settings[k] = v

    return config
