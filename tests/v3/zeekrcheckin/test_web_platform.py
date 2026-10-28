"""Offline WEB_PC/App routing regression; unsigned synthetic JWTs only."""
import base64
import json
import pathlib
import sys
import time
import unittest
from unittest.mock import patch

ROOT=pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'tools'))
from stub_host import load_plugin
plugin=load_plugin('zeekr_web_platform_tests')

def fixture(platform=None,device='0',object_sub=False):
    def part(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')
    login={'lastLoginDeviceId':device}
    if platform is not None:login['platform']=platform
    sub={'accountInfoDTO':{'accountId':'2000000000000000001'},'accountLoginInfoDTO':login}
    return 'Bearer '+part({'alg':'none'})+'.'+part({'sub':sub if object_sub else json.dumps(sub),'exp':int(time.time())+864000})+'.offline'

class WebPlatformTest(unittest.TestCase):
    def test_parse_source_platform_for_string_and_object_sub(self):
        for source in ['WEB_PC','IOS','ANDROID',None,'WEB_PC_EVIL','WEB_PC\n']:
            for object_sub in [False,True]:
                with self.subTest(source=source,object_sub=object_sub):
                    info=plugin.parse_token(fixture(source,object_sub=object_sub))
                    self.assertEqual(info.get('sourcePlatform'),source or '')
                    self.assertEqual(info['accountId'],'2000000000000000001')

    def test_web_numeric_and_string_device_zero_are_not_missing(self):
        for device in ['0',0]:
            self.assertEqual(plugin.parse_token(fixture('WEB_PC',device))['loginDeviceId'],'0')

    def test_only_exact_web_pc_adds_header(self):
        inst=plugin.ZeekrCheckin()
        for source in ['WEB_PC','IOS','ANDROID','','WEB_PC_EVIL','WEB_PC\n']:
            ctx={'token':fixture(source),'deviceId':'existing-device','appVersion':'4.9.33','sourcePlatform':source}
            headers=inst._headers(ctx)
            self.assertEqual(headers['platform'],'WEB_PC' if source=='WEB_PC' else '')
            self.assertEqual(headers['device_id'],'existing-device')
            self.assertEqual(headers['platform_h5'],'IOS')

    def test_per_account_flow_does_not_leak_web_identity_into_app(self):
        inst=plugin.ZeekrCheckin()
        inst._app_version='4.9.33'
        inst._device_id=''
        inst._steps='off'
        seen=[]
        def request(ctx,path,method='GET',body=None):
            headers=inst._headers(ctx)
            expected='WEB_PC' if json.loads(base64.urlsafe_b64decode(ctx['token'].split('.')[1]+'=='))['sub'].find('WEB_PC')>=0 else ''
            seen.append(headers['platform'])
            return {'code':'000000' if headers['platform']==expected else '000009','data':{}}
        inst._request=request
        inst._do_tasks=lambda *args:[]
        inst._get_tasks=lambda *args:[]
        with patch.object(plugin.time,'sleep',lambda seconds:None):
            for source in ['WEB_PC','IOS','WEB_PC','ANDROID',None]:
                result=inst._run_one(fixture(source),'sign','offline')
                self.assertTrue(result['ok'],result['lines'])
        self.assertEqual(seen,['WEB_PC','','WEB_PC','',''])

if __name__=='__main__':unittest.main()
