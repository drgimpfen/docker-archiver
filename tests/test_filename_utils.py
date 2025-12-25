import re
import glob
import os
from app import utils


def test_filename_timestamp_format():
    ts = utils.filename_timestamp()
    assert re.match(r'^\d{8}_\d{6}$', ts)


def test_filename_safe_and_combination():
    safe = utils.filename_safe('My App/Name*')
    assert safe == 'My_App_Name'
    ts = utils.filename_timestamp()
    fn = f"job_123_{safe}_{ts}.log"
    assert 'My_App_Name' in fn and fn.endswith('.log') and re.search(r'\d{8}_\d{6}\.log$', fn)


def test_cleanup_match_latest(tmp_path):
    d = tmp_path
    f1 = d / 'cleanup_42_20250101_000000.log'
    f2 = d / 'cleanup_42_20250102_000000.log'
    f1.write_text('a')
    f2.write_text('b')
    matches = glob.glob(str(d / 'cleanup_42_*.log'))
    assert max(matches, key=os.path.getmtime).endswith('20250102_000000.log')
