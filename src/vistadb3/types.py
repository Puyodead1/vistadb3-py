"""Column type codes as stored in VistaDB 3.5 files (they match the public VistaDBType enum)."""
from enum import IntEnum


class VType(IntEnum):
    CHAR = 1
    NCHAR = 2
    VARCHAR = 3
    NVARCHAR = 4
    TEXT = 5
    NTEXT = 6
    TINYINT = 8
    SMALLINT = 9
    INT = 10
    BIGINT = 11
    REAL = 12
    FLOAT = 13
    DECIMAL = 14
    MONEY = 15
    SMALLMONEY = 16
    BIT = 17
    DATETIME = 19
    IMAGE = 20
    VARBINARY = 21
    UNIQUEIDENTIFIER = 22
    SMALLDATETIME = 23
    TIMESTAMP = 24


# Fixed on-disk width of the fixed-size types.
FIXED_SIZE = {
    VType.TINYINT: 1,
    VType.SMALLINT: 2,
    VType.INT: 4,
    VType.BIGINT: 8,
    VType.REAL: 4,
    VType.FLOAT: 8,
    VType.DECIMAL: 14,
    VType.MONEY: 8,
    VType.SMALLMONEY: 4,
    VType.BIT: 1,
    VType.DATETIME: 8,
    VType.UNIQUEIDENTIFIER: 16,
    VType.SMALLDATETIME: 4,
    VType.TIMESTAMP: 8,
}

STRING_TYPES = {VType.CHAR, VType.NCHAR, VType.VARCHAR, VType.NVARCHAR}
BLOB_TYPES = {VType.TEXT, VType.NTEXT, VType.IMAGE}
TEXT_TYPES = STRING_TYPES | {VType.TEXT, VType.NTEXT}
UNICODE_TYPES = {VType.NCHAR, VType.NVARCHAR, VType.NTEXT}

# SQL spelling used for CREATE TABLE and in schema dumps.
SQL_NAME = {t: t.name for t in VType}
SQL_NAME[VType.INT] = "INT"
SQL_NAME[VType.UNIQUEIDENTIFIER] = "UNIQUEIDENTIFIER"

# Public names as reported by the VistaDB provider (VistaDBType.ToString()).
PUBLIC_NAME = {
    VType.CHAR: "Char", VType.NCHAR: "NChar", VType.VARCHAR: "VarChar", VType.NVARCHAR: "NVarChar",
    VType.TEXT: "Text", VType.NTEXT: "NText", VType.TINYINT: "TinyInt", VType.SMALLINT: "SmallInt",
    VType.INT: "Int", VType.BIGINT: "BigInt", VType.REAL: "Real", VType.FLOAT: "Float",
    VType.DECIMAL: "Decimal", VType.MONEY: "Money", VType.SMALLMONEY: "SmallMoney", VType.BIT: "Bit",
    VType.DATETIME: "DateTime", VType.IMAGE: "Image", VType.VARBINARY: "VarBinary",
    VType.UNIQUEIDENTIFIER: "UniqueIdentifier", VType.SMALLDATETIME: "SmallDateTime",
    VType.TIMESTAMP: "Timestamp",
}
BY_PUBLIC_NAME = {v.lower(): k for k, v in PUBLIC_NAME.items()}
