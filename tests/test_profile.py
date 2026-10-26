import sys, pathlib, tempfile, unittest
from decimal import Decimal
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from calculate import sha256_file, volume_profile
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
if __name__=='__main__':unittest.main()

