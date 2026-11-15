import argparse
import csv
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, TypeVar


class DataError(ValueError):
    pass


def parse_date(value: str) -> date:
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise ValueError(f"некорректная дата {value!r}, ожидается ГГГГ-ММ-ДД")


def optional_date(value: str) -> date | None:
    return parse_date(value) if value else None


def kopecks(value: str) -> int:
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", value):
        raise ValueError(f"некорректная сумма {value!r}, ожидаются рубли с точностью до копейки")
    rubles, _, cents = value.partition(".")
    return int(rubles) * 100 + int(cents.ljust(2, "0"))


@dataclass(frozen=True)
class Contract:
    contract_id: str
    contractor: str
    amount: int
    signed_at: date


@dataclass(frozen=True)
class Work:
    work_id: str
    contract_id: str
    title: str
    price: int
    completed_at: date | None
    cancelled_at: date | None

    def status_on(self, as_of: date) -> str:
        if self.cancelled_at is not None and self.cancelled_at <= as_of:
            return "cancelled"
        if self.completed_at is not None and self.completed_at <= as_of:
            return "done"
        return "planned"


@dataclass(frozen=True)
class Payment:
    payment_id: str
    contract_id: str
    work_id: str
    amount: int
    paid_at: date | None
    refunded_at: date | None

    def counts_on(self, as_of: date) -> bool:
        return self.paid_at is not None and self.paid_at <= as_of and (
            self.refunded_at is None or as_of < self.refunded_at
        )


def read_contract(row: dict[str, str]) -> Contract:
    return Contract(
        row["contract_id"], row["contractor"], kopecks(row["amount"]), parse_date(row["signed_at"])
    )


def read_work(row: dict[str, str]) -> Work:
    completed = optional_date(row["completed_at"])
    cancelled = optional_date(row["cancelled_at"])
    dates_by_status = {"planned": (False, False), "done": (True, False), "cancelled": (False, True)}
    if dates_by_status.get(row["status"]) != (completed is not None, cancelled is not None):
        raise ValueError(f"статус {row['status']!r} не соответствует датам работы")
    return Work(row["work_id"], row["contract_id"], row["title"], kopecks(row["price"]), completed, cancelled)


def read_payment(row: dict[str, str]) -> Payment:
    created = parse_date(row["created_at"])
    paid = optional_date(row["paid_at"])
    refunded = optional_date(row["refunded_at"])
    dates_by_status = {"queued": (False, False), "paid": (True, False), "refunded": (True, True)}
    if dates_by_status.get(row["status"]) != (paid is not None, refunded is not None):
        raise ValueError(f"статус {row['status']!r} не соответствует датам платежа")
    if paid is not None and (paid < created or (refunded is not None and refunded < paid)):
        raise ValueError("нарушен порядок дат создания, оплаты и возврата платежа")
    return Payment(row["payment_id"], row["contract_id"], row["work_id"], kopecks(row["amount"]), paid, refunded)


Record = TypeVar("Record", Contract, Work, Payment)


def read_csv(
    path: Path,
    columns: str,
    optional: set[str],
    convert: Callable[[dict[str, str]], Record],
) -> dict[str, Record]:
    fields = columns.split()
    id_column = fields[0]
    required_values = [field for field in fields if field not in optional]
    records = {}
    try:
        with path.open(encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source, strict=True)
            header = reader.fieldnames or []
            missing = set(fields) - set(header)
            if len(header) != len(set(header)) or missing:
                raise DataError(f"{path.name}: ожидаются уникальные колонки {', '.join(fields)}")
            for row in reader:
                try:
                    if None in row or any(value is None for value in row.values()):
                        raise ValueError("число значений не совпадает с числом колонок")
                    row = {key: value.strip() for key, value in row.items()}
                    for field in required_values:
                        if not row[field]:
                            raise ValueError(f"пустое обязательное поле {field}")
                    record_id = row[id_column]
                    if record_id in records:
                        raise ValueError(f"повторяется {id_column} {record_id!r}")
                    records[record_id] = convert(row)
                except ValueError as error:
                    raise DataError(f"{path.name}, строка {reader.line_num}: {error}") from error
    except (OSError, UnicodeError, csv.Error) as error:
        raise DataError(f"{path.name}: {error}") from error
    return records


def load_data(directory: Path) -> tuple[list[Contract], list[Work], list[Payment]]:
    contracts = read_csv(
        directory / "contracts.csv", "contract_id contractor amount signed_at", set(), read_contract
    )
    works = read_csv(
        directory / "works.csv", "work_id contract_id title price status completed_at cancelled_at",
        {"completed_at", "cancelled_at"}, read_work,
    )
    payments = read_csv(
        directory / "payments.csv",
        "payment_id contract_id work_id purpose amount status created_at paid_at refunded_at",
        {"work_id", "purpose", "paid_at", "refunded_at"}, read_payment,
    )
    for work in works.values():
        if work.contract_id not in contracts:
            raise DataError(f"works.csv, {work.work_id}: неизвестный договор {work.contract_id!r}")
    for payment in payments.values():
        if payment.contract_id not in contracts:
            raise DataError(f"payments.csv, {payment.payment_id}: неизвестный договор {payment.contract_id!r}")
        if payment.work_id:
            work = works.get(payment.work_id)
            if work is None or work.contract_id != payment.contract_id:
                raise DataError(
                    f"payments.csv, {payment.payment_id}: работа {payment.work_id!r} "
                    f"отсутствует в договоре {payment.contract_id!r}"
                )
    return list(contracts.values()), list(works.values()), list(payments.values())


@dataclass(frozen=True)
class WorkReport:
    work: Work
    status: str
    direct: int
    allocated: int

    @property
    def paid(self) -> int:
        return self.direct + self.allocated

    @property
    def remaining(self) -> int:
        return 0 if self.status == "cancelled" else max(self.work.price - self.paid, 0)


@dataclass(frozen=True)
class ContractReport:
    contract: Contract
    works: list[WorkReport]
    paid: int
    unallocated: int


def build_report(
    contracts: list[Contract], works: list[Work], payments: list[Payment], as_of: date
) -> list[ContractReport]:
    works_by_contract: dict[str, list[Work]] = defaultdict(list)
    for work in works:
        works_by_contract[work.contract_id].append(work)

    direct: dict[str, int] = defaultdict(int)
    common: dict[str, int] = defaultdict(int)
    paid: dict[str, int] = defaultdict(int)
    for payment in payments:
        if payment.counts_on(as_of):
            paid[payment.contract_id] += payment.amount
            if payment.work_id:
                direct[payment.work_id] += payment.amount
            else:
                common[payment.contract_id] += payment.amount

    report = []
    for contract in sorted(contracts, key=lambda item: item.contract_id):
        if contract.signed_at > as_of:
            continue
        rows = works_by_contract[contract.contract_id]
        pool = common[contract.contract_id]
        allocations = {}
        accepted = sorted(
            (work for work in rows if work.status_on(as_of) == "done"),
            key=lambda work: (work.completed_at, work.work_id),
        )
        for work in accepted:
            allocated = min(pool, max(work.price - direct[work.work_id], 0))
            allocations[work.work_id] = allocated
            pool -= allocated

        work_reports = [
            WorkReport(work, work.status_on(as_of), direct[work.work_id], allocations.get(work.work_id, 0))
            for work in sorted(rows, key=lambda item: item.work_id)
        ]
        report.append(ContractReport(contract, work_reports, paid[contract.contract_id], pool))
    return report


def money(amount: int) -> str:
    rubles, cents = divmod(amount, 100)
    grouped_rubles = f"{rubles:,}".replace(",", " ")
    return f"{grouped_rubles},{cents:02d}"


def cell(value: str) -> str:
    value = " ".join(value.splitlines())
    return re.sub(r"([\\|`*_\[\]<>&~])", r"\\\1", value)


def table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(row) + " |" for row in [headers, ["---"] * len(headers), *rows]]
    return "\n".join(lines)


def render_report(report: list[ContractReport], as_of: date) -> str:
    title = f"# Отчёт на {as_of:%d.%m.%Y} включительно"
    if not report:
        return f"{title}\n\nНа эту дату нет подписанных договоров.\n"

    statuses = {"done": "Принята", "planned": "Запланирована", "cancelled": "Отменена"}
    work_rows = []
    contract_rows = []
    for item in report:
        contract = item.contract
        balance = contract.amount - item.paid
        contract_rows.append([
            cell(contract.contract_id), cell(contract.contractor), money(contract.amount),
            money(sum(row.work.price for row in item.works if row.status != "cancelled")),
            money(item.paid), money(sum(row.paid for row in item.works)), money(item.unallocated),
            money(sum(row.remaining for row in item.works if row.status == "done")),
            money(sum(row.remaining for row in item.works)), money(max(balance, 0)), money(max(-balance, 0)),
        ])
        for row in item.works:
            work_rows.append([
                cell(contract.contract_id), cell(row.work.work_id), cell(row.work.title), statuses[row.status],
                money(row.work.price), money(row.direct), money(row.allocated), money(row.paid),
                money(row.remaining),
            ])
    works = table(
        ["Договор", "Работа", "Название", "Статус", "Цена", "Прямые платежи", "Из общих платежей",
         "Выплачено", "Осталось"],
        work_rows,
    ) if work_rows else "В выгрузке нет работ по этим договорам."
    contracts = table(
        ["Договор", "Подрядчик", "Сумма договора", "Цена неотменённых работ", "Выплачено всего",
         "Отнесено на работы", "Нераспределено", "Осталось по принятым", "Осталось по всем работам",
         "Остаток по сумме договора", "Сверх суммы договора"],
        contract_rows,
    )
    return f"{title}\n\nВсе суммы в рублях.\n\n## Работы\n\n{works}\n\n## Договоры\n\n{contracts}\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Отчёт по работам и договорам на выбранную дату.")
    parser.add_argument("--as-of", required=True, metavar="ГГГГ-ММ-ДД", help="дата отчёта включительно")
    parser.add_argument(
        "--data-dir", type=Path, default=Path(__file__).resolve().parent / "data",
        help="каталог с исходными CSV (по умолчанию data рядом со скриптом)",
    )
    args = parser.parse_args()
    try:
        as_of = parse_date(args.as_of)
    except ValueError as error:
        parser.error(str(error))
    try:
        contracts, works, payments = load_data(args.data_dir)
    except DataError as error:
        print(f"Ошибка данных: {error}", file=sys.stderr)
        return 1
    print(render_report(build_report(contracts, works, payments, as_of), as_of), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
