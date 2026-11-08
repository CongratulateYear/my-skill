#!/usr/bin/env python3
"""Explicit fine-export enrichment, in place, with an external immutable backup.

Only existing 商品id/商品标题 cells are updated. ERP 商品名称 is the title,
not 颜色及规格. Unique, complete, valid ERP product pairs are authoritative.
Other workbook XML/ZIP entries are preserved, including formula caches/styles.
No report sheet, extra columns or result workbook is generated; reports are JSON.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Iterable
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape
import zipfile

from openpyxl import load_workbook


class MatchError(ValueError):
    """Unsafe or ambiguous input; original workbook is not replaced."""


FINE_ID = ('主订单编号', '主订单号')
ORDER_ID = ('原始线上订单号',)
TITLE = ('商品标题',)
PRODUCT = ('商品id', '商品ID')
ORDER_TITLE = ('商品名称',)
ORDER_PRODUCT = ('店铺款式编码',)
SCI = re.compile(r'^[+-]?(?:\d+(?:\.\d*)?|\.\d+)[eE][+-]?\d+$')
SPLIT = re.compile(r'[,，;；\r\n]+')


def _empty(value):
    return value is None or (isinstance(value, str) and not value.strip())


def _label(value):
    return value.strip() if isinstance(value, str) else value


def _select(wb, groups, sheet=None, preferred=None):
    matches = []
    if sheet is not None and sheet not in wb.sheetnames:
        raise MatchError(f'Sheet not found: {sheet}')
    for ws in ([wb[sheet]] if sheet else wb.worksheets):
        candidates = []
        for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 50)):
            positions = {}
            for cell in row:
                positions.setdefault(_label(cell.value), []).append(cell.column)
            if all(any(alias in positions for alias in group) for group in groups):
                cols = []
                for group in groups:
                    found = [col for alias in group for col in positions.get(alias, [])]
                    if len(found) != 1:
                        raise MatchError(f'Ambiguous headers in {ws.title}, row {row[0].row}: {group}')
                    cols.append(found[0])
                candidates.append((ws, row[0].row, cols))
        if len(candidates) > 1:
            raise MatchError(f'Multiple header rows in {ws.title}')
        matches.extend(candidates)
    if preferred and not sheet:
        preferred_matches = [item for item in matches if item[0].title == preferred]
        if preferred_matches:
            return preferred_matches[0]
    if len(matches) != 1:
        raise MatchError('Expected one matching sheet/header; specify sheet explicitly')
    return matches[0]


def _ids(cell):
    value = cell.value
    where = f'{cell.parent.title}!{cell.coordinate}'
    if _empty(value):
        return []
    if cell.data_type == 'f':
        raise MatchError(f'Formula order ID rejected: {where}')
    if isinstance(value, bool):
        raise MatchError(f'Boolean order ID rejected: {where}')
    if isinstance(value, (int, float)):
        if not math.isfinite(value) or int(value) != value:
            raise MatchError(f'Noninteger order ID rejected: {where}')
        if len(str(abs(int(value)))) > 15:
            raise MatchError(f'Numeric order ID exceeds 15 digits (precision loss): {where}')
        if re.search(r'[eE][+-]?0', cell.number_format):
            raise MatchError(f'Scientific order ID rejected: {where}')
        if value < 0:
            raise MatchError(f'Negative order ID rejected: {where}')
        return [str(int(value))]
    if not isinstance(value, str):
        raise MatchError(f'Unsupported order ID type: {where}')
    result = []
    for token in SPLIT.split(value):
        token = token.strip()
        if not token:
            continue
        if SCI.fullmatch(token):
            raise MatchError(f'Scientific order ID rejected: {where}')
        if token.startswith(('=', '+', '-', '@')) or any(c.isspace() for c in token):
            raise MatchError(f'Unsafe or malformed order ID rejected: {where}')
        if token not in result:
            result.append(token)
    return result


def extract_order_ids(path, sheet=None):
    """Read fine main-order IDs as exact strings; fail closed on lossy inputs."""
    wb = load_workbook(path, data_only=False)
    try:
        ws, header, cols = _select(wb, [FINE_ID], sheet, '处罚数据')
        result = {}
        for row in ws.iter_rows(min_row=header + 1):
            for value in _ids(row[cols[0] - 1]):
                result.setdefault(value, None)
        return list(result)
    finally:
        wb.close()


def _product_text(cell):
    value = cell.value
    if cell.data_type in ('f', 'e') or isinstance(value, bool):
        raise MatchError(f'Invalid ERP product field: {cell.coordinate}')
    if _empty(value):
        raise MatchError(f'Empty ERP product field: {cell.coordinate}')
    if isinstance(value, (int, float)):
        if not math.isfinite(value) or int(value) != value or len(str(abs(int(value)))) > 15:
            raise MatchError(f'Unsafe numeric product field: {cell.coordinate}; export as text')
        value = str(int(value))
    elif not isinstance(value, str):
        raise MatchError(f'Invalid ERP product type: {cell.coordinate}')
    if len(value) > 32767 or any(ord(c) < 32 and c not in '\t\n\r' for c in value):
        raise MatchError(f'Invalid ERP product text: {cell.coordinate}')
    return value


def _shop_name(name):
    if (not isinstance(name, str) or not name.strip() or name != name.strip()
            or name in ('.', '..') or any(c in name for c in '/\\:*?"<>|')
            or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or name.endswith(('.', ' '))):
        raise MatchError('Unsafe shop_name: use a plain filename component, no paths')
    return name


def _source_path(path):
    path = Path(path).expanduser().absolute()
    for part in [path, *path.parents]:
        if part.is_symlink() and str(part) not in ('/tmp', '/var'):
            raise MatchError(f'Symlink workbook path rejected: {part}')
    path = path.resolve()
    if not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
        raise MatchError(f'Expected regular workbook: {path}')
    return path


def _backup_directory(fine_path, backup_dir):
    # Sibling of the merge input directory, never a child of that directory.
    directory = (Path(backup_dir).expanduser().absolute() if backup_dir is not None
                 else __import__('tgc_runtime').runtime_for(fine_path) / 'backups')
    # Canonicalize the platform's /tmp alias, but reject user-controlled symlinks.
    for part in [directory, *directory.parents]:
        if part.is_symlink() and str(part) not in ('/tmp', '/var'):
            raise MatchError(f'Symlink backup directory rejected: {part}')
    directory = directory.resolve()
    if directory == fine_path.parent or fine_path.parent in directory.parents:
        raise MatchError('backup_dir must be outside the fine input directory tree')
    return directory


def _sheet_member(archive, title):
    ns = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    book = ET.fromstring(archive.read('xl/workbook.xml'))
    rels = ET.fromstring(archive.read('xl/_rels/workbook.xml.rels'))
    sheet = next(s for s in book.find('m:sheets', ns) if s.attrib['name'] == title)
    rel_id = sheet.attrib['{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id']
    target = next(r.attrib['Target'] for r in rels if r.attrib['Id'] == rel_id)
    import posixpath
    return posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'xl/' + target)


def _patch_sheet(data, changes):
    """Replace only target cell XML; other bytes (including formulas) stay exact."""
    text = data.decode('utf-8')
    # Standard OOXML uses an unprefixed default worksheet namespace. Fail closed
    # on unusual prefixed worksheets rather than risking an unrelated mutation.
    if not re.search(r'<worksheet\b', text):
        raise MatchError('Unsupported worksheet XML namespace prefix')
    for coord, value in changes.items():
        pattern = re.compile(r'<c\b(?=[^>]*\br="' + re.escape(coord) + r'")[^>]*(?:/>|>.*?</c>)', re.S)
        found = list(pattern.finditer(text))
        if len(found) > 1:
            raise MatchError(f'Duplicate worksheet cell: {coord}')
        attrs = f' r="{coord}"'
        if found:
            opening = found[0].group().split('>', 1)[0]
            attrs = opening[2:].rstrip('/')
            attrs = re.sub(r'\s+t="[^"]*"', '', attrs)
        replacement = '<c' + attrs + ' t="inlineStr"><is><t xml:space="preserve">' + escape(value).replace('\r', '&#13;') + '</t></is></c>'
        if found:
            text = text[:found[0].start()] + replacement + text[found[0].end():]
        else:
            row_number = re.search(r'\d+$', coord).group()
            row_pattern = re.compile(r'(<row\b(?=[^>]*\br="' + row_number + r'")[^>]*>)(.*?)(</row>)', re.S)
            row = row_pattern.search(text)
            if row is None:
                raise MatchError(f'Missing worksheet row for {coord}')
            from openpyxl.utils.cell import coordinate_from_string, column_index_from_string
            col = column_index_from_string(coordinate_from_string(coord)[0])
            body = row.group(2)
            insertion = len(body)
            for cell in re.finditer(r'<c\b[^>]*\br="([A-Z]+)\d+"', body):
                if column_index_from_string(cell.group(1)) > col:
                    insertion = cell.start()
                    break
            body = body[:insertion] + replacement + body[insertion:]
            text = text[:row.start(2)] + body + text[row.end(2):]
    return text.encode('utf-8')


def _lock_output(lock):
    """Hold a kernel advisory lock; a crash leaves only a reusable inert file.

    Never unlink this inode: removing it while another opener waits could let
    two writers lock distinct files under the same path.
    """
    if lock.is_symlink():
        raise MatchError(f'Symlink output lock rejected: {lock}')
    flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
    try:
        fd = os.open(lock, flags, 0o600)
    except OSError as exc:
        raise MatchError(f'Cannot open output lock: {lock}') from exc
    stream = os.fdopen(fd, 'r+b', buffering=0)
    try:
        opened = os.fstat(fd)
        current = lock.lstat()
        if (not stat.S_ISREG(opened.st_mode) or stat.S_ISLNK(current.st_mode)
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
            raise MatchError(f'Unsafe output lock: {lock}')
        if os.name == 'nt':
            import msvcrt
            if opened.st_size == 0:
                stream.write(b'0')
            stream.seek(0)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return stream
    except BaseException as exc:
        stream.close()
        if isinstance(exc, OSError):
            raise MatchError(f'Output locked: {lock}') from exc
        raise


def _unlock_output(stream):
    try:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        stream.close()


def _publish(original_bytes, destination, sheet, changes, backup_dir):
    from tgc_runtime import runtime_for
    lock = runtime_for(destination) / 'match.lock'
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock_stream = _lock_output(lock)
    temp = None
    try:
        if destination.is_symlink() or destination.read_bytes() != original_bytes:
            raise MatchError('Original workbook changed during matching')
        fd, name = tempfile.mkstemp(prefix='.' + destination.stem + '-', suffix='.xlsx', dir=destination.parent.parent)
        os.close(fd)
        temp = Path(name)
        with zipfile.ZipFile(io.BytesIO(original_bytes)) as source, zipfile.ZipFile(temp, 'w') as target:
            if len(source.namelist()) != len(set(source.namelist())):
                raise MatchError('Duplicate ZIP entries rejected')
            member = _sheet_member(source, sheet)
            for info in source.infolist():
                data = source.read(info.filename)
                target.writestr(info, _patch_sheet(data, changes) if info.filename == member else data)
            target.comment = source.comment
        # Validate complete workbook, all non-target archive entries, and targets.
        with zipfile.ZipFile(io.BytesIO(original_bytes)) as source, zipfile.ZipFile(temp) as target:
            if target.namelist() != source.namelist() or target.testzip() is not None:
                raise MatchError('Validation failed: workbook archive changed')
            for name in source.namelist():
                if name != member and source.read(name) != target.read(name):
                    raise MatchError(f'Validation failed: original member changed: {name}')
        check = load_workbook(temp, data_only=False)
        try:
            for coord, value in changes.items():
                if check[sheet][coord].value != value or check[sheet][coord].data_type != 's':
                    raise MatchError(f'Validation failed: target value: {coord}')
        finally:
            check.close()
        with temp.open('r+b') as handle:
            os.fsync(handle.fileno())
        backup_dir.mkdir(parents=True, exist_ok=True)
        # A content-addressed backup is reused only after exact byte validation.
        digest = hashlib.sha256(original_bytes).hexdigest()
        backup = backup_dir / f'{destination.stem}-{digest}.xlsx'
        try:
            fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if backup.is_symlink() or not backup.is_file() or backup.read_bytes() != original_bytes:
                raise MatchError(f'Unsafe or conflicting backup exists: {backup}')
        else:
            try:
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(original_bytes)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                backup.unlink(missing_ok=True)
                raise
        if destination.is_symlink() or destination.read_bytes() != original_bytes:
            raise MatchError('Original workbook changed during save')
        os.chmod(temp, stat.S_IMODE(destination.stat().st_mode))
        os.replace(temp, destination)
        temp = None
        return str(backup)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
        _unlock_output(lock_stream)


def match_fines(fine_path, order_paths: Iterable, shop_name, trigger, *, sheet=None,
                order_sheet=None, output=None, backup_dir=None):
    """Update the original fine workbook; return JSON-ready counts/row report.

    output is retained for API compatibility but must name fine_path, if supplied.
    Incomplete/invalid ERP pairs preserve both originals; duplicate identical
    pairs are unambiguous. Missing coverage or different pairs never guess.
    """
    if trigger != 'fine-export':
        raise MatchError("Matching requires explicit trigger='fine-export'")
    _shop_name(shop_name)
    fine_path = _source_path(fine_path)
    if fine_path.suffix.lower() != '.xlsx':
        raise MatchError('Fine workbook must be .xlsx')
    if output is not None:
        candidate = Path(output).expanduser().absolute()
        if candidate.is_symlink() or candidate.resolve() != fine_path:
            raise MatchError('Matching is in-place only; output must equal fine_path')
    backup_directory = _backup_directory(fine_path, backup_dir)
    if isinstance(order_paths, (str, Path)):
        order_paths = [order_paths]
    paths = [_source_path(p) for p in order_paths]
    if not paths or fine_path in paths:
        raise MatchError('At least one separate freshly downloaded order workbook is required')
    index, invalid, erp_issues = {}, {}, []
    for path in paths:
        orders = load_workbook(path, data_only=False)
        try:
            ws, header, cols = _select(orders, [ORDER_ID, ORDER_TITLE, ORDER_PRODUCT], order_sheet)
            for row in ws.iter_rows(min_row=header + 1):
                ids = _ids(row[cols[0] - 1])
                if not ids:
                    continue
                try:
                    pair = tuple(_product_text(row[col - 1]) for col in cols[1:])
                except MatchError as exc:
                    erp_issues.append({'file': str(path), 'sheet': ws.title, 'row': row[0].row,
                                       'reason': str(exc), 'reason_code': 'invalid_fields'})
                    for order_id in ids:
                        invalid.setdefault(order_id, []).append(str(exc))
                    continue
                for order_id in ids:
                    index.setdefault(order_id, set()).add(pair)
        finally:
            orders.close()
    original_bytes = fine_path.read_bytes()
    wb = load_workbook(io.BytesIO(original_bytes), data_only=False)
    try:
        ws, header, cols = _select(wb, [FINE_ID, TITLE, PRODUCT], sheet, '处罚数据')
        changes = {}
        counts = dict(matched=0, unmatched=0, ambiguous=0, invalid=0, conflict=0,
                      filled_cells=0, overwritten_cells=0, changed_cells=0)
        details = []
        # Only explicit amount headers; formulas/unparseable values remain unknown.
        from decimal import Decimal, InvalidOperation
        amount_columns = [col for col in range(1, ws.max_column + 1)
                          if str(ws.cell(header, col).value or '').strip() in
                          ('罚款金额', '处罚金额', '扣款金额', '金额')]
        review_amount, known_amount_rows, unknown_amount_rows = Decimal('0'), 0, 0
        reason_counts = dict(no_order_id=0, erp_missing=0, ambiguous=0,
                             invalid_fields=0, matched=0)
        for row_no in range(header + 1, ws.max_row + 1):
            if all(_empty(ws.cell(row_no, col).value) for col in range(1, ws.max_column + 1)):
                continue
            ids = _ids(ws.cell(row_no, cols[0]))
            candidates = set().union(*(index.get(order_id, set()) for order_id in ids)) if ids else set()
            status, reason = 'unmatched', 'No order ID or one/more IDs absent from export; originals preserved'
            reason_code = 'erp_missing' if ids else 'no_order_id'
            if any(order_id in invalid for order_id in ids):
                status, reason = 'invalid', 'Empty/invalid ERP product field; both originals preserved'
                reason_code = 'invalid_fields'
            elif ids and all(order_id in index for order_id in ids):
                if len(candidates) != 1:
                    status, reason = 'ambiguous', 'Multiple distinct ERP product pairs; originals preserved'
                    reason_code = 'ambiguous'
                else:
                    status, reason = 'matched', 'Unique valid ERP product pair is authoritative for both fields'
                    reason_code = 'matched'
                    title, product = next(iter(candidates))
                    for col, value in zip((cols[1], cols[2]), (title, product)):
                        cell = ws.cell(row_no, col)
                        if cell.value != value or cell.data_type != 's':
                            changes[cell.coordinate] = value
                            counts['changed_cells'] += 1
                            counts['filled_cells' if _empty(cell.value) else 'overwritten_cells'] += 1
            counts[status] += 1
            reason_counts[reason_code] += 1
            detail = {'row': row_no, 'order_ids': ids, 'status': status,
                      'reason': reason, 'reason_code': reason_code,
                      'candidate_pairs': len(candidates)}
            if status != 'matched':
                amount = None
                if len(amount_columns) == 1:
                    cell = ws.cell(row_no, amount_columns[0])
                    if cell.data_type not in ('f', 'e') and not isinstance(cell.value, bool):
                        try:
                            amount = Decimal(str(cell.value).replace(',', '').strip())
                            if not amount.is_finite(): amount = None
                        except (InvalidOperation, ValueError):
                            pass
                if amount is None:
                    unknown_amount_rows += 1
                else:
                    review_amount += amount
                    known_amount_rows += 1
                    detail['amount'] = str(amount)
            details.append(detail)
        backup = (_publish(original_bytes, fine_path, ws.title, changes, backup_directory)
                  if changes else None)
        return {'output': str(fine_path), 'counts': counts, 'sheet': ws.title,
                'backup': backup, 'changed': bool(changes), 'report': details,
                'reason_counts': reason_counts,
                'review_amount': str(review_amount) if known_amount_rows else None,
                'review_amount_known_rows': known_amount_rows,
                'review_amount_unknown_rows': unknown_amount_rows,
                'erp_issues': erp_issues, 'report_sheet': None, 'added_columns': []}
    finally:
        wb.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    extract = commands.add_parser('extract', help='Extract exact fine order IDs as JSON')
    extract.add_argument('fine_path')
    extract.add_argument('--sheet')
    match = commands.add_parser('match', help='Match in place only after fine-export')
    match.add_argument('--fine', required=True)
    match.add_argument('--orders', nargs='+', required=True)
    match.add_argument('--shop-name', required=True)
    match.add_argument('--trigger', choices=['fine-export'], required=True)
    match.add_argument('--sheet')
    match.add_argument('--order-sheet')
    match.add_argument('--output', help='Compatibility alias; must equal --fine')
    match.add_argument('--backup-dir', help='Backup directory outside the fine input folder tree')
    args = parser.parse_args(argv)
    try:
        result = (extract_order_ids(args.fine_path, args.sheet) if args.command == 'extract' else
                  match_fines(args.fine, args.orders, args.shop_name, args.trigger,
                              sheet=args.sheet, order_sheet=args.order_sheet, output=args.output,
                              backup_dir=args.backup_dir))
    except (MatchError, OSError, KeyError) as exc:
        parser.exit(2, f'Error: {exc}\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
