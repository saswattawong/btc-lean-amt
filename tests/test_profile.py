import sys, pathlib, tempfile, unittest
from decimal import Decimal
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from calculate import sha256_file, volume_profile
from render_brief import render
class ProfileTest(unittest.TestCase):
    def test_bucket_vwap_and_70percent(self):
        rows=[(Decimal('100.5'),Decimal('6')),(Decimal('110.2'),Decimal('3')),(Decimal('120.1'),Decimal('1'))]
        r=volume_profile(rows,Decimal('10'))
        self.assertEqual(r['poc_usdt'],100)
        self.assertEqual(r['val_usdt'],100)
        self.assertEqual(r['vah_usdt'],120)
        self.assertAlmostEqual(r['vwap_usdt'],105.37)
        self.assertEqual(r['aggregated_trade_rows'],3)
    def test_nonexistent_volume_gap(self):
        rows=[(Decimal('100'),Decimal('4')),(Decimal('130'),Decimal('6'))]
        r=volume_profile(rows,Decimal('10'))
        self.assertEqual(r['poc_usdt'],130)
        self.assertGreaterEqual(r['value_area_actual_share'],.70)
    def test_sha256_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path=pathlib.Path(directory)/'sample.bin';path.write_bytes(b'abc')
            self.assertEqual(sha256_file(path),'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad')
    def test_dashboard_brief_rejects_misaligned_dates(self):
        data={
          'as_of_closed_day_utc':'2026-10-25','report_reference_date_utc':'2026-10-26',
          'generated_at_utc':'2026-10-26T05:46:39+00:00',
          'integrity_cross_check':{'status':'passed'},
          'daily_trade_volume_profile':{'vah_usdt':110,'val_usdt':90,'poc_usdt':100,'vwap_usdt':101,
                                        'value_area_actual_share':.7},
          'daily_poc_migration':{'from_day_utc':'2026-10-23','to_day_utc':'2026-10-24',
                                 'previous_poc_usdt':90,'current_poc_usdt':100,'direction':'higher','delta_usdt':10},
          'previous_completed_week_trade_volume_profile':{'from_utc':'2026-10-19','to_utc':'2026-10-25',
              'vah_usdt':120,'val_usdt':80,'poc_usdt':100,'vwap_usdt':101,
              'value_area_actual_share':.71,'integrity_status':'passed'},
          'vwap':{'developing_month_vwap_to_last_closed_utc_day':101,
                  'previous_completed_month_vwap':99,'rolling_365_complete_utc_days_vwap':95}}
        with self.assertRaisesRegex(ValueError,'migration date'):
            render(data)
if __name__=='__main__':unittest.main()

