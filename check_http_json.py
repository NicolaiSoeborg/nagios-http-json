#!/usr/bin/env python3

"""
Check HTTP JSON Nagios Plugin

Generic Nagios plugin which checks json values from a given endpoint against argument specified rules
and determines the status and performance data for that service.
"""

import urllib.error, urllib.parse, base64
import json
import argparse
import sys
from pprint import pprint
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


OK_CODE = 0
WARNING_CODE = 1
CRITICAL_CODE = 2
UNKNOWN_CODE = 3


def TypeHelper(value, field_type):
    def get_size(b, system):
        for factor, suffix in system:
            if b >= factor:
                break
        amount = int(b/factor)
        return str(amount) + suffix

    if field_type == 'size':
        return get_size(int(value), [(1024 ** 5, ' PiB'), (1024 ** 4, ' TiB'), (1024 ** 3, ' GiB'), (1024 ** 2, ' MiB'), (1024 ** 1, ' KiB'), (1024 ** 0, ' bytes')])
    if field_type.lower() == 'si':
        return get_size(int(value), [(1000 ** 5, 'P'), (1000 ** 4, 'T'), (1000 ** 3, 'G'), (1000 ** 2, 'M'), (1000 ** 1, 'K'), (1000 ** 0, 'B')])

    assert(field_type == 'str')
    return str(value)

class NagiosHelper:
    """Help with Nagios specific status string formatting."""
    message_prefixes = {OK_CODE: 'OK', WARNING_CODE: 'WARNING', CRITICAL_CODE: 'CRITICAL', UNKNOWN_CODE: 'UNKNOWN'}
    status_message = ''
    performance_data = ''
    warning_message = ''
    critical_message = ''
    unknown_message = ''

    def getMessage(self):
        """Build a status-prefixed message with optional performance data generated externally"""
        text = "%s:%s" % (self.message_prefixes[self.getCode()], self.status_message)
        text += self.critical_message
        text += self.warning_message
        text += self.unknown_message
        if self.performance_data:
            text += "|%s" % self.performance_data
        return text

    def getCode(self):
        code = OK_CODE
        if (self.warning_message != ''):
            code = WARNING_CODE
        if (self.critical_message != ''):
            code = CRITICAL_CODE
        if (self.unknown_message != ''):
            code = UNKNOWN_CODE
        return code

    def append_warning(self, warning_message, status_message):
        self.warning_message += warning_message
        self.status_message += status_message

    def append_critical(self, critical_message, status_message):
        self.critical_message += critical_message
        self.status_message += status_message

    def append_unknown(self, unknown_message, status_message):
        self.unknown_message += unknown_message
        self.status_message += status_message

    def append_metrics(self, performance_data, warning_message, critical_message):
        self.performance_data += performance_data
        self.append_warning(warning_message, '')
        self.append_critical(critical_message, '')

class JsonHelper:
    """Perform simple comparison operations against values in a given JSON dict"""
    def __init__(self, json_data, separator):
        self.data = json_data
        self.separator = separator
        self.arrayOpener = '('
        self.arrayCloser = ')'

    def getSubElement(self, key, data):
        separatorIndex = key.find(self.separator)
        partialKey = key[:separatorIndex]
        remainingKey = key[separatorIndex + 1:]
        if partialKey in data:
            return self.get(remainingKey, data[partialKey])
        else:
            return (None, 'not_found')

    def getSubArrayElement(self, key, data):
        subElemKey = key[:key.find(self.arrayOpener)]
        k = key[key.find(self.arrayOpener) + 1:key.find(self.arrayCloser)]
        if not k.isdecimal():  # isdigit() for py2
            # k is of type: name=val
            # val might be e.g. "Hadoop:service=Resource Manager (Stuff),name=RMNMInfo",
            # which is a hell to parse, so we base64 en/decode val to make parsing easier.

            # find index in data where "data[name] = base64.decode(val)":
            n, v = k.split("=", 1)
            v = base64.b64decode(v).decode()
            for i in range(len(data)):
                if str(self.get(n, data[i])) == str(v):
                    k = i
                    break
            if not isinstance(k, int): # did we find index of k?
                return (None, 'not_found')

        index = int(k)
        remainingKey = key[key.find(self.arrayCloser + self.separator) + 2:]
        if key.find(self.arrayCloser + self.separator) == -1:
            remainingKey = key[key.find(self.arrayCloser) + 1:]
        if subElemKey in data:
            if index < len(data[subElemKey]):
                return self.get(remainingKey, data[subElemKey][index])
            else:
                return (None, 'not_found')
        else:
            if not subElemKey:
                return self.get(remainingKey, data[index])
            else:
                return (None, 'not_found')

    def equals(self, key, value): return self.exists(key) and str(self.get(key)) in value.split(':')
    def lte(self, key, value): return self.exists(key) and float(self.get(key)) <= float(value)
    def lt(self, key, value): return self.exists(key) and float(self.get(key)) < float(value)
    def gte(self, key, value): return self.exists(key) and float(self.get(key)) >= float(value)
    def gt(self, key, value): return self.exists(key) and float(self.get(key)) > float(value)
    def exists(self, key): return (self.get(key) != (None, 'not_found'))
    def get(self, key, temp_data=''):
        """Can navigate nested json keys with a dot format (Element.Key.NestedKey). Returns (None, 'not_found') if not found"""
        if temp_data:
            data = temp_data
        else:
            data = self.data
        if len(key) <= 0:
            return data
        if key.find(self.separator) != -1 and key.find(self.arrayOpener) != -1:
            if key.find(self.separator) < key.find(self.arrayOpener):
                return self.getSubElement(key, data)
            else:
                return self.getSubArrayElement(key, data)
        else:
            if key.find(self.separator) != -1:
                return self.getSubElement(key, data)
            else:
                if key.find(self.arrayOpener) != -1:
                    return self.getSubArrayElement(key, data)
                else:
                    if key in data:
                        return data[key]
                    else:
                        return (None, 'not_found')

def _getKeyAlias(original_key):
    key = original_key
    alias = original_key
    if '>' in original_key:
        keys = original_key.split('>')
        if len(keys) == 2:
            key, alias = keys
    return key, alias

class JsonRuleProcessor:
    """Perform checks and gather values from a JSON dict given rules and metrics definitions"""
    def __init__(self, json_data, rules_args):
        self.data = json_data
        self.rules = rules_args
        separator = '.'
        if self.rules.separator: separator = self.rules.separator
        self.helper = JsonHelper(self.data, separator)
        debugPrint(rules_args.debug, "rules:%s" % rules_args)
        debugPrint(rules_args.debug, "separator:%s" % separator)

    def checkExists(self, exists_list):
        failure, success = '', ''
        for k in exists_list:
            key, alias = _getKeyAlias(k)
            if (self.helper.exists(key) == False):
                failure += " Key %s did not exist." % alias
            # else:
            #    success += " Key %s does exist." % alias
        return (failure, success)

    def checkEquality(self, equality_list):
        failure, success = '', ''
        for kv in equality_list:
            k, v = kv.split(',')
            key, alias = _getKeyAlias(k)
            key_val = TypeHelper(self.helper.get(key), self.rules.field_type)
            if (self.helper.equals(key, v) == False):
                failure += " Value for key %s (%s) did not match %s." % (alias, key_val, v)
            else:
                success += " Value for key %s (%s) does match %s." % (alias, key_val, v)
        return (failure, success)

    def checkThreshold(self, key, alias, r):
        failure, success = '', ''
        invert = False
        start = 0
        end = 'infinity'
        if r.startswith('@'):
            invert = True
            r = r[1:]
        vals = r.split(':')
        if len(vals) == 1:
            end = vals[0]
        if len(vals) == 2:
            start = vals[0]
            if vals[1] != '':
                end = vals[1]
        th = lambda x: TypeHelper(x, self.rules.field_type)
        key_val = th(self.helper.get(key))
        invert_str = ''
        if invert: invert_str = 'not'
        if start == '~':
            if invert and self.helper.lte(key, end):
                failure += " Value for key %s (%s) was less than or equal to %s." % (alias, key_val, th(end))
            elif not invert and self.helper.gt(key, end):
                failure += " Value for key %s (%s) was greater than %s." % (alias, key_val, th(end))
            else:
                success += " Value for key %s (%s) was%s in range ':%s'." % (alias, key_val, invert_str, th(end))
        elif end == 'infinity':
            if invert and self.helper.gte(key, start):
                failure += " Value for key %s (%s) was greater than or equal to %s." % (alias, key_val, th(start))
            elif (not invert and self.helper.lt(key, start)):
                failure += " Value for key %s (%s) was less than %s." % (alias, key_val, th(start))
            else:
                success += " Value for key %s (%s) was%s in range '%s:'." % (alias, key_val, invert_str, th(start))
        else:
            if invert and self.helper.gte(key, start) and self.helper.lte(key, end):
                failure += " Value for key %s (%s) was inside the range '%s : %s'." % (alias, key_val, th(start), th(end))
            elif not invert and (self.helper.lt(key, start) or self.helper.gt(key, end)):
                failure += " Value for key %s (%s) was outside the range '%s : %s'." % (alias, key_val, th(start), th(end))
            else:
                success += " Value for key %s (%s) was%s in range '%s : %s'." % (alias, key_val, invert_str, th(start), th(end))
        return (failure, success)

    def checkThresholds(self, threshold_list):
        failure, success = '', ''
        for threshold in threshold_list:
            k, r = threshold.split(',')
            key, alias = _getKeyAlias(k)
            result = self.checkThreshold(key, alias, r)
            failure += result[0]
            success += result[1]
        return (failure, success)

    def checkWarning(self):
        failure, success = '', ''
        if self.rules.key_threshold_warning != None:
            result = self.checkThresholds(self.rules.key_threshold_warning)
            failure += result[0]
            success += result[1]
        if self.rules.key_value_list != None:
            result = self.checkEquality(self.rules.key_value_list)
            failure += result[0]
            success += result[1]
        if self.rules.key_list != None:
            result = self.checkExists(self.rules.key_list)
            failure += result[0]
            success += result[1]
        return (failure, success)

    def checkCritical(self):
        failure, success = '', ''
        if self.rules.key_threshold_critical != None:
            result = self.checkThresholds(self.rules.key_threshold_critical)
            failure += result[0]
            success += result[1]
        if self.rules.key_value_list_critical != None:
            result = self.checkEquality(self.rules.key_value_list_critical)
            failure += result[0]
            success += result[1]
        if self.rules.key_list_critical != None:
            result = self.checkExists(self.rules.key_list_critical)
            failure += result[0]
            success += result[1]
        return (failure, success)

    def checkMetrics(self):
        """Return a Nagios specific performance metrics string given keys and parameter definitions"""
        metrics = ''
        warning = ''
        critical = ''
        if self.rules.metric_list != None:
            for metric in self.rules.metric_list:
                key = metric
                minimum = maximum = warn_range = crit_range = None
                uom = ''
                if ',' in metric:
                    vals = metric.split(',')
                    if len(vals) == 2:
                        key, uom = vals
                    if len(vals) == 4:
                        key, uom, warn_range, crit_range = vals
                    if len(vals) == 6:
                        key, uom, warn_range, crit_range, minimum, maximum = vals
                key, alias = _getKeyAlias(key)
                if self.helper.exists(key):
                    metrics += "'%s'=%s" % (alias, self.helper.get(key))
                    if uom: metrics += uom
                    if warn_range != None:
                        warning += self.checkThreshold(key, alias, warn_range)[0]
                        metrics += ";%s" % warn_range
                    if crit_range != None:
                        critical += self.checkThreshold(key, alias, crit_range)[0]
                        metrics += ";%s" % crit_range
                    if minimum != None:
                        critical += self.checkThreshold(key, alias, minimum + ':')[0]
                        metrics += ";%s" % minimum
                    if maximum != None:
                        critical += self.checkThreshold(key, alias, '~:' + maximum)[0]
                        metrics += ";%s" % maximum
                metrics += ' '
        return ["%s" % metrics, warning, critical]

def parseArgs():
    parser = argparse.ArgumentParser(description=
        'Nagios plugin which checks json values from a given endpoint against argument specified rules\
        and determines the status and performance data for that service\n' + """
Examples:

    ./check_http_json.py --host localhost --port 8088 --path jmx --warning "beans.(0).val,10:20"

Will test if the json at http://localhost:8088/jmx has an element like  {beans:[{val: 15}]}  

If you can't be sure of the location in the json of some value, you can search using (name=value), but this is somewhat limited, so the value part has to be base64 encoded.
E.g. if you want to find the element in a list where "name" is equal to "java.lang:type=MemoryPool (raw),name=Metaspace" you have to encode the value part to:

python -c "import base64; print(base64.b64encode('java.lang:type=MemoryPool (raw),name=Metaspace'))" and get "amF2YS5sYW5nOnR5cGU9TWVtb3J5UG9vbCAocmF3KSxuYW1lPU1ldGFzcGFjZQ==".
You can now query a unknown json tree using:

    ./chech_http_json.py --host localhost --port 8088 --part jmx --warning "beans.(name=amF2YS5sYW5nOnR5cGU9TWVtb3J5UG9vbCAocmF3KSxuYW1lPU1ldGFzcGFjZQ==).someProp,RANGE"
""")

    # parser.add_argument('-v', '--verbose', action='store_true', help='Verbose Output')
    parser.add_argument('-d', '--debug', action='store_true', help='Debug mode.')
    parser.add_argument('-s', '--ssl', action='store_true', help='HTTPS mode.')
    parser.add_argument('-H', '--host', dest='host', required=True, help='Host.')
    parser.add_argument('-P', '--port', dest='port', help='TCP port')
    parser.add_argument('-p', '--path', dest='path', help='Path.')
    parser.add_argument('-t', '--timeout', type=int, help='Connection timeout (seconds)')
    parser.add_argument('-B', '--basic-auth', dest='auth', help='Basic auth string "username:password"')
    parser.add_argument('-D', '--data', dest='data', help='The http payload to send as a POST')
    parser.add_argument('-A', '--headers', dest='headers', help='The http headers in JSON format.')
    parser.add_argument('-f', '--field_separator', dest='separator',
        help='Json Field separator, defaults to "." ; Select element in an array with "(" ")" ; Use "(" name = base64(val) ")" for finding keys matching a certain value.')
    parser.add_argument('-ft', '--field_type', dest='field_type', help='Treat numbers as <type>, e.g. "str|size|SI".', default = 'str')
    parser.add_argument('-w', '--warning', dest='key_threshold_warning', nargs='*',
        help='Warning threshold for these values (key1[>alias],WarnRange key2[>alias],WarnRange). WarnRange is in the format [@]start:end, more information at nagios-plugins.org/doc/guidelines.html.')
    parser.add_argument('-c', '--critical', dest='key_threshold_critical', nargs='*',
        help='Critical threshold for these values (key1[>alias],CriticalRange key2[>alias],CriticalRange. CriticalRange is in the format [@]start:end, more information at nagios-plugins.org/doc/guidelines.html.')
    parser.add_argument('-e', '--key_exists', dest='key_list', nargs='*',
        help='Checks existence of these keys to determine status. Return warning if key is not present.')
    parser.add_argument('-E', '--key_exists_critical', dest='key_list_critical', nargs='*',
        help='Same as -e but return critical if key is not present.')
    parser.add_argument('-q', '--key_equals', dest='key_value_list', nargs='*',
        help='Checks equality of these keys and values (key[>alias],value key2,value2) to determine status.\
        Multiple key values can be delimited with colon (key,value1:value2). Return warning if equality check fails')
    parser.add_argument('-Q', '--key_equals_critical', dest='key_value_list_critical', nargs='*',
        help='Same as -q but return critical if equality check fails.')
    parser.add_argument('-m', '--key_metric', dest='metric_list', nargs='*',
        help='Gathers the values of these keys (key[>alias],UnitOfMeasure,WarnRange,CriticalRange,Min,Max) for Nagios performance data.\
        More information about Range format and units of measure for nagios can be found at nagios-plugins.org/doc/guidelines.html\
        Additional formats for this parameter are: (key[>alias]), (key[>alias],UnitOfMeasure), (key[>alias],UnitOfMeasure,WarnRange,CriticalRange).')

    return parser.parse_args()

def debugPrint(debug_flag, message, pretty_flag=False):
    if debug_flag:
        if pretty_flag:
            pprint(message)
        else:
            print(message)

""" TODO: Add tests to a seperate file? """
if __name__ == "__main__" and len(sys.argv) >= 2 and sys.argv[1] == 'UnitTest':
    import unittest
    class RulesHelper:
        field_type = 'str'
        separator = '.'
        debug = False
        key_threshold_warning, key_value_list, key_list, key_threshold_critical, \
        key_value_list_critical, key_list_critical, metric_list = None, None, None, None, None, None, None

        def dash_m(self, data):
            self.metric_list = data
            return self

        def dash_e(self, data):
            self.key_list = data
            return self

        def dash_E(self, data):
            self.key_list_critical = data
            return self

        def dash_q(self, data):
            self.key_value_list = data
            return self

        def dash_Q(self, data):
            self.key_value_list_critical = data
            return self

        def dash_w(self, data):
            self.key_threshold_warning = data
            return self

        def dash_c(self, data):
            self.key_threshold_critical = data
            return self


    class UnitTest(unittest.TestCase):
        rules = RulesHelper()

        def check_data(self, args, jsondata, code):
            data = json.loads(jsondata)
            nagios = NagiosHelper()
            processor = JsonRuleProcessor(data, args)
            nagios.append_warning(*processor.checkWarning())
            nagios.append_critical(*processor.checkCritical())
            nagios.append_metrics(*processor.checkMetrics())
            self.assertEqual(code, nagios.getCode())

        def test_metrics(self):
            self.check_data(RulesHelper().dash_m(['metric,,1:4,1:5']), '{"metric": 5}', WARNING_CODE)
            self.check_data(RulesHelper().dash_m(['metric,,1:5,1:4']), '{"metric": 5}', CRITICAL_CODE)
            self.check_data(RulesHelper().dash_m(['metric,,1:5,1:5,6,10']), '{"metric": 5}', CRITICAL_CODE)
            self.check_data(RulesHelper().dash_m(['metric,,1:5,1:5,1,4']), '{"metric": 5}', CRITICAL_CODE)
            self.check_data(RulesHelper().dash_m(['metric,s,@1:4,@6:10,1,10']), '{"metric": 5}', OK_CODE)

        def test_exists(self):
            self.check_data(RulesHelper().dash_e(['nothere']), '{"metric": 5}', WARNING_CODE)
            self.check_data(RulesHelper().dash_E(['nothere']), '{"metric": 5}', CRITICAL_CODE)
            self.check_data(RulesHelper().dash_e(['metric']), '{"metric": 5}', OK_CODE)

        def test_equality(self):
            self.check_data(RulesHelper().dash_q(['metric,6']), '{"metric": 5}', WARNING_CODE)
            self.check_data(RulesHelper().dash_Q(['metric,6']), '{"metric": 5}', CRITICAL_CODE)
            self.check_data(RulesHelper().dash_q(['metric,5']), '{"metric": 5}', OK_CODE)

        def test_warning_thresholds(self):
            self.check_data(RulesHelper().dash_w(['metric,5']), '{"metric": 5}', OK_CODE)
            self.check_data(RulesHelper().dash_w(['metric,5:']), '{"metric": 5}', OK_CODE)
            self.check_data(RulesHelper().dash_w(['metric,~:5']), '{"metric": 5}', OK_CODE)
            self.check_data(RulesHelper().dash_w(['metric,1:5']), '{"metric": 5}', OK_CODE)
            self.check_data(RulesHelper().dash_w(['metric,@5']), '{"metric": 6}', OK_CODE)
            self.check_data(RulesHelper().dash_w(['metric,@5:']), '{"metric": 4}', OK_CODE)
            self.check_data(RulesHelper().dash_w(['metric,@~:5']), '{"metric": 6}', OK_CODE)
            self.check_data(RulesHelper().dash_w(['metric,@1:5']), '{"metric": 6}', OK_CODE)
            self.check_data(RulesHelper().dash_w(['metric,5']), '{"metric": 6}', WARNING_CODE)
            self.check_data(RulesHelper().dash_w(['metric,5:']), '{"metric": 4}', WARNING_CODE)
            self.check_data(RulesHelper().dash_w(['metric,~:5']), '{"metric": 6}', WARNING_CODE)
            self.check_data(RulesHelper().dash_w(['metric,1:5']), '{"metric": 6}', WARNING_CODE)
            self.check_data(RulesHelper().dash_w(['metric,@5']), '{"metric": 5}', WARNING_CODE)
            self.check_data(RulesHelper().dash_w(['metric,@5:']), '{"metric": 5}', WARNING_CODE)
            self.check_data(RulesHelper().dash_w(['metric,@~:5']), '{"metric": 5}', WARNING_CODE)
            self.check_data(RulesHelper().dash_w(['metric,@1:5']), '{"metric": 5}', WARNING_CODE)

        def test_critical_thresholds(self):
            self.check_data(RulesHelper().dash_c(['metric,5']), '{"metric": 5}', OK_CODE)
            self.check_data(RulesHelper().dash_c(['metric,5:']), '{"metric": 5}', OK_CODE)
            self.check_data(RulesHelper().dash_c(['metric,~:5']), '{"metric": 5}', OK_CODE)
            self.check_data(RulesHelper().dash_c(['metric,1:5']), '{"metric": 5}', OK_CODE)
            self.check_data(RulesHelper().dash_c(['metric,@5']), '{"metric": 6}', OK_CODE)
            self.check_data(RulesHelper().dash_c(['metric,@5:']), '{"metric": 4}', OK_CODE)
            self.check_data(RulesHelper().dash_c(['metric,@~:5']), '{"metric": 6}', OK_CODE)
            self.check_data(RulesHelper().dash_c(['metric,@1:5']), '{"metric": 6}', OK_CODE)
            self.check_data(RulesHelper().dash_c(['metric,5']), '{"metric": 6}', CRITICAL_CODE)
            self.check_data(RulesHelper().dash_c(['metric,5:']), '{"metric": 4}', CRITICAL_CODE)
            self.check_data(RulesHelper().dash_c(['metric,~:5']), '{"metric": 6}', CRITICAL_CODE)
            self.check_data(RulesHelper().dash_c(['metric,1:5']), '{"metric": 6}', CRITICAL_CODE)
            self.check_data(RulesHelper().dash_c(['metric,@5']), '{"metric": 5}', CRITICAL_CODE)
            self.check_data(RulesHelper().dash_c(['metric,@5:']), '{"metric": 5}', CRITICAL_CODE)
            self.check_data(RulesHelper().dash_c(['metric,@~:5']), '{"metric": 5}', CRITICAL_CODE)
            self.check_data(RulesHelper().dash_c(['metric,@1:5']), '{"metric": 5}', CRITICAL_CODE)

        def test_separator(self):
            rules = RulesHelper()
            rules.separator = '_'
            self.check_data(
                rules.dash_q(['(0)_gauges_jvm.buffers.direct.capacity(1)_value,1234']),
                '[{ "gauges": { "jvm.buffers.direct.capacity": [{"value": 215415},{"value": 1234}]}}]',
                OK_CODE)
    unittest.main()
    exit(0)


"""Program entry point"""
if __name__ == "__main__":
    args = parseArgs()
    nagios = NagiosHelper()
    if args.ssl:
        url = "https://%s" % args.host
    else:
        url = "http://%s" % args.host
    if args.port: url += ":%s" % args.port
    if args.path: url += "/%s" % args.path
    debugPrint(args.debug, "url:%s" % url)
    # Attempt to reach the endpoint
    try:
        req = Request(url)
        if args.auth:
            base64str = base64.encodestring(args.auth).replace('\n', '')
            req.add_header('Authorization', 'Basic %s' % base64str)
        if args.headers:
            headers=json.loads(args.headers)
            debugPrint(args.debug, "Headers:\n %s" % headers)
            for header in headers:
                req.add_header(header, headers[header])
        if args.timeout and args.data:
            response = urlopen(req, timeout=args.timeout, data=args.data)
        elif args.timeout:
            response = urlopen(req, timeout=args.timeout)
        elif args.data:
            response = urlopen(req, data=args.data)
        else:
            response = urlopen(req)
    except HTTPError as e:
        nagios.append_unknown("HTTPError[%s], url:%s" % (str(e.code), url), '')
    except URLError as e:
        nagios.append_critical("URLError[%s], url:%s" % (str(e.reason), url), '')
    else:
        jsondata = response.read().decode()
        data = json.loads(jsondata)
        debugPrint(args.debug, 'json:')
        debugPrint(args.debug, data, True)
        # Apply rules to returned JSON data
        processor = JsonRuleProcessor(data, args)
        nagios.append_warning(*processor.checkWarning())
        nagios.append_critical(*processor.checkCritical())
        nagios.append_metrics(*processor.checkMetrics())
    # Print Nagios specific string and exit appropriately
    print(nagios.getMessage())
    exit(nagios.getCode())
