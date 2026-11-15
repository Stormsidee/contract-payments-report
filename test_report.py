import csv
import shutil
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from report import Contract, DataError, Payment, Work, build_report, kopecks, load_data, render_report


DATA = Path(__file__).resolve().parent / "data"


class ReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = load_data(DATA)

    def snapshot(self, day):
        return {
            report.contract.contract_id: report
            for report in build_report(*self.source, date.fromisoformat(day))
        }

    def test_control_date(self):
        contracts = self.snapshot("2026-11-10")
        self.assertEqual(set(contracts), {"D-1", "D-2"})
        first, second = contracts["D-1"], contracts["D-2"]
        self.assertEqual((first.paid, first.unallocated), (31_500_000, 1_500_000))
        self.assertEqual((second.paid, second.unallocated), (7_500_000, 2_500_000))
        expected = {
            "W-11": ("done", 0, 10_000_000, 0),
            "W-12": ("done", 0, 10_000_000, 0),
            "W-13": ("cancelled", 0, 0, 0),
            "W-14": ("done", 1_500_000, 8_500_000, 0),
            "W-15": ("planned", 0, 0, 10_000_000),
            "W-16": ("planned", 0, 0, 10_000_000),
            "W-21": ("done", 0, 2_000_000, 0),
            "W-22": ("done", 0, 3_000_000, 0),
            "W-23": ("planned", 0, 0, 4_000_000),
        }
        for contract in contracts.values():
            self.assertEqual(contract.paid, sum(row.paid for row in contract.works) + contract.unallocated)
            for row in contract.works:
                self.assertEqual((row.status, row.direct, row.allocated, row.remaining), expected[row.work.work_id])

    def test_dates_of_signing_refund_and_cancellation(self):
        self.assertEqual(self.snapshot("2026-08-24"), {})
        self.assertEqual(self.snapshot("2026-08-25")["D-1"].paid, 0)
        self.assertEqual(self.snapshot("2026-09-09")["D-1"].paid, 35_000_000)
        self.assertEqual(self.snapshot("2026-09-10")["D-1"].paid, 30_000_000)
        self.assertEqual(self.snapshot("2026-10-11")["D-1"].works[2].status, "planned")
        self.assertEqual(self.snapshot("2026-10-12")["D-1"].works[2].status, "cancelled")

    def test_next_acceptance_and_payment(self):
        accepted = self.snapshot("2026-11-11")["D-2"]
        self.assertEqual((accepted.paid, accepted.unallocated), (7_500_000, 0))
        self.assertEqual((accepted.works[-1].status, accepted.works[-1].remaining), ("done", 1_500_000))
        paid = self.snapshot("2026-11-12")["D-2"]
        self.assertEqual((paid.paid, paid.unallocated, paid.works[-1].remaining), (9_500_000, 500_000, 0))
        rendered = render_report([paid], date(2026, 11, 12))
        self.assertIn("| 90 000,00 | 90 000,00 | 95 000,00 | 90 000,00 | 5 000,00 | "
                      "0,00 | 0,00 | 0,00 | 5 000,00 |", rendered)

    def test_direct_payments_are_applied_before_common_payments(self):
        first_day, second_day = date(2026, 11, 1), date(2026, 11, 2)
        contracts = [Contract("D", "Подрядчик", 500, first_day)]
        works = [
            Work("A", "D", "Принята позже", 100, second_day, None),
            Work("C", "D", "Принята в один день с B", 150, first_day, None),
            Work("B", "D", "Принята в один день с C", 100, first_day, None),
            Work("X", "D", "Отменена", 100, None, second_day),
            Work("Y", "D", "Будущая", 100, None, None),
        ]
        payments = [
            Payment("P1", "D", "", 120, first_day, None),
            Payment("P2", "D", "B", 25, first_day, None),
            Payment("P3", "D", "A", 101, second_day, None),
            Payment("P4", "D", "X", 30, first_day, None),
            Payment("P5", "D", "Y", 50, first_day, None),
        ]
        (report,) = build_report(contracts, works, payments, second_day)
        self.assertEqual([row.allocated for row in report.works], [0, 75, 45, 0, 0])
        self.assertEqual([row.paid for row in report.works], [101, 100, 45, 30, 50])
        self.assertEqual([row.remaining for row in report.works], [0, 0, 105, 0, 50])
        self.assertEqual((report.paid, report.unallocated), (326, 0))

    def test_money_is_exact(self):
        self.assertEqual(kopecks("0.10") + kopecks("0.20"), kopecks("0.30"))
        self.assertEqual(kopecks("2.1"), 210)
        with self.assertRaises(ValueError):
            kopecks("1.001")

    def test_payment_to_another_contract_fails(self):
        with TemporaryDirectory() as name:
            directory = Path(name) / "data"
            shutil.copytree(DATA, directory)
            path = directory / "payments.csv"
            with path.open(encoding="utf-8", newline="") as source:
                rows = list(csv.reader(source))
            rows[1][2] = "W-21"
            with path.open("w", encoding="utf-8", newline="") as target:
                csv.writer(target).writerows(rows)
            with self.assertRaisesRegex(DataError, r"payments\.csv, P-1.*W-21.*D-1"):
                load_data(directory)


if __name__ == "__main__":
    unittest.main()
