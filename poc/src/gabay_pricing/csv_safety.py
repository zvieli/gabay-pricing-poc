"""Protect spreadsheet consumers from formulas in externally supplied text."""


def safe_cell(value):
    if isinstance(value, str) and value.lstrip(' \t\r\n').startswith(('=', '+', '-', '@')):
        return "'" + value
    return value


class SafeWriter:
    def __init__(self, writer):
        self.writer = writer

    def writeheader(self):
        return self.writer.writeheader()

    def writerow(self, row):
        if isinstance(row, dict):
            row = {key: safe_cell(value) for key, value in row.items()}
        else:
            row = [safe_cell(value) for value in row]
        return self.writer.writerow(row)
