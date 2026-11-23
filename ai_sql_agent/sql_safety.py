"""Parse and constrain generated analytics SQL before passing it to a driver."""

from __future__ import annotations

import string
from collections.abc import Collection

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, SqlglotError
from sqlglot.optimizer.scope import Scope, traverse_scope
from sqlglot.tokens import TokenType


class UnsafeQueryError(ValueError):
    """The statement did not pass the analytics policy."""

    retryable = False


class UnknownRelationError(UnsafeQueryError):
    """The requested table or view is absent from the discovered schema."""

    retryable = True


class QuerySyntaxError(UnsafeQueryError):
    """The statement could not be parsed for the selected SQL dialect."""

    retryable = True


DIALECTS = {"sqlite": "sqlite", "mysql": "mysql", "postgresql": "postgres"}
_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)

_BLOCKED_NODES = (
    exp.DDL, exp.DML, exp.Command, exp.Into, exp.Lock, exp.Hint, exp.PropertyEQ,
    exp.Parameter, exp.Placeholder, exp.SessionParameter, exp.Execute, exp.Set,
    exp.NextValueFor, exp.Transaction, exp.UserDefinedFunction,
)
_WRITE_TOKENS = frozenset({
    TokenType.ALTER, TokenType.ATTACH, TokenType.COMMAND, TokenType.COPY, TokenType.CREATE,
    TokenType.DELETE, TokenType.DETACH, TokenType.DROP, TokenType.EXECUTE, TokenType.GRANT,
    TokenType.INSERT, TokenType.INTO, TokenType.LOAD, TokenType.MERGE, TokenType.PRAGMA,
    TokenType.REVOKE, TokenType.ROLLBACK, TokenType.SET, TokenType.UPDATE, TokenType.USE,
})

# SQLGlot normalizes some dialect functions to names such as TIME_TO_STR.
_COMMON_FUNCTIONS = frozenset("""
ABS ACOS ARRAY ARRAY_AGG ARRAY_CONCAT ARRAY_LENGTH ARRAY_SIZE ARRAY_TO_STRING
ASIN ATAN ATAN2 AVG BITWISE_AND_AGG BITWISE_OR_AGG BITWISE_XOR_AGG CAST CBRT CEIL
CHR COALESCE CONCAT CONCAT_WS CORR COS COT COUNT COUNT_IF CUME_DIST CURRENT_DATE
CURRENT_TIME CURRENT_TIMESTAMP DATE DATE_ADD DATE_DIFF DATE_FROM_PARTS DATE_STR_TO_DATE
DATE_SUB DATE_TRUNC DATETIME DAY DAY_OF_MONTH DAY_OF_WEEK DAY_OF_YEAR DECODE
DENSE_RANK DIV ENCODE ENDS_WITH EXISTS EXP EXTRACT FIRST_VALUE FLOOR FORMAT FORMAT_DATE
FROM_ISO8601_DATE GENERATE_SERIES GREATEST GROUP_CONCAT GROUPING HEX IF INITCAP
INSTR IS_INF IS_NAN JSON JSON_ARRAY JSON_ARRAY_AGG JSON_ARRAY_LENGTH JSON_CONTAINS
JSON_EXTRACT JSON_EXTRACT_SCALAR JSON_OBJECT JSON_OBJECT_AGG JSON_TYPE JSON_VALID
LAG LAST_DAY LAST_VALUE LEAD LEAST LEFT LENGTH LN LOG LOG10 LOG2 LOWER LPAD LTRIM
MAKE_DATE MAKE_INTERVAL MAX MD5 MEDIAN MIN MOD MONTH NORMALIZE NTH_VALUE NTILE
NULLIF NUMBER_TO_STR OCTET_LENGTH OVERLAY PERCENT_RANK PERCENTILE_CONT PERCENTILE_DISC
POSITION POW QUANTILE QUARTER RANDOM RAND RANK REGEXP_EXTRACT REGEXP_LIKE
REGEXP_REPLACE REPEAT REPLACE REVERSE RIGHT ROUND ROW_NUMBER RPAD RTRIM SIGN SIN
SPLIT SPLIT_PART SQRT STARTS_WITH STDDEV STDDEV_POP STDDEV_SAMP STR_POSITION
STR_TO_DATE STR_TO_TIME SUBSTRING SUM TAN TIME TIME_TO_STR TIME_TO_UNIX TIMESTAMP
TIMESTAMP_ADD TIMESTAMP_DIFF TIMESTAMP_SUB TIMESTAMP_TRUNC TIMESTAMPDIFF TO_CHAR
TO_NUMBER TO_TIMESTAMP TRIM TRUNC TRUNCATE TS_OR_DS_ADD TS_OR_DS_DIFF TS_OR_DS_TO_DATE
TS_OR_DS_TO_TIME TS_OR_DS_TO_TIMESTAMP TYPEOF UNHEX UNIX_TO_TIME UNNEST UPPER
VARIANCE VAR_POP VAR_SAMP WEEK WEEK_OF_YEAR YEAR
""".split())

_DIALECT_FUNCTIONS = {
    "sqlite": frozenset("""
        DATE DATETIME GLOB IIF INSTR JULIANDAY LIKELIHOOD LIKELY PRINTF QUOTE
        SOUNDEX STRFTIME TIME TOTAL UNICODE UNIXEPOCH UNLIKELY ZEROBLOB
    """.split()),
    "mysql": frozenset("""
        ADDDATE ADDTIME ASCII CHAR_LENGTH CONVERT_TZ CURDATE CURTIME DATE_FORMAT
        DAYNAME DAYOFMONTH DAYOFWEEK DAYOFYEAR FIELD FIND_IN_SET FORMAT FROM_DAYS
        FROM_UNIXTIME HOUR IFNULL JSON_LENGTH LOCATE MAKEDATE MAKETIME MICROSECOND
        MINUTE MONTHNAME NOW PERIOD_ADD PERIOD_DIFF SEC_TO_TIME SECOND SPACE
        STR_TO_DATE SUBDATE SUBTIME SYSDATE TIME_FORMAT TIME_TO_SEC TIMEDIFF TO_DAYS
        TO_SECONDS UNIX_TIMESTAMP UTC_DATE UTC_TIME UTC_TIMESTAMP WEEKDAY YEARWEEK
    """.split()),
    "postgresql": frozenset("""
        AGE ASCII BTRIM CARDINALITY CHAR_LENGTH CLOCK_TIMESTAMP DATE_BIN DATE_PART
        FACTORIAL FORMAT ISFINITE JSONB_AGG JSONB_ARRAY_LENGTH JSONB_BUILD_ARRAY
        JSONB_BUILD_OBJECT JSONB_EXTRACT_PATH JSONB_EXTRACT_PATH_TEXT JSONB_OBJECT_AGG
        JSONB_PRETTY JSONB_TYPEOF JSON_BUILD_ARRAY JSON_BUILD_OBJECT JSON_EACH
        JSON_EXTRACT_PATH JSON_EXTRACT_PATH_TEXT JSON_TYPEOF LOCALTIME LOCALTIMESTAMP
        MAKE_TIME MAKE_TIMESTAMP MAKE_TIMESTAMPTZ MIN_SCALE NOW PG_TYPEOF REGEXP_COUNT
        REGEXP_MATCH REGEXP_MATCHES REGEXP_SUBSTR REGR_AVGX REGR_AVGY REGR_COUNT
        REGR_INTERCEPT REGR_R2 REGR_SLOPE ROW STATEMENT_TIMESTAMP STRING_AGG
        STRING_TO_ARRAY TIMEOFDAY TO_ASCII TO_DATE TO_JSON TO_JSONB TRANSLATE
    """.split()),
}


def prepare_query(
    sql: str,
    dialect: str,
    *,
    schema: str,
    tables: Collection[str],
    row_limit: int,
) -> str:
    """Return generated, comments-free SQL with scoped relations and a server row cap."""
    if dialect not in DIALECTS:
        raise ValueError("Unsupported database dialect.")
    if not sql or not sql.strip() or len(sql) > 80_000 or "\x00" in sql:
        raise UnsafeQueryError("Enter one nonempty SELECT query of at most 80,000 characters.")

    sqlglot_dialect = DIALECTS[dialect]
    try:
        statements = sqlglot.parse(sql, read=sqlglot_dialect, error_level=ErrorLevel.RAISE)
    except RecursionError:
        raise UnsafeQueryError("The query is nested too deeply. Use fewer nested expressions.") from None
    except SqlglotError as exc:
        if not _looks_like_single_read_query(sql, sqlglot_dialect):
            raise UnsafeQueryError("Run a single read-only SELECT or SELECT with a CTE.") from None
        # The parser includes the full user/model SQL in its detailed errors.
        detail = getattr(exc, "errors", [])
        location = ""
        if detail:
            line = detail[0].get("line")
            column = detail[0].get("col")
            location = f" near line {line}, column {column}"
        raise QuerySyntaxError(f"SQL could not be parsed for {dialect}{location}.") from None

    statements = [item for item in statements if item is not None and not isinstance(item, exp.Semicolon)]
    if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.SetOperation)):
        raise UnsafeQueryError("Run a single read-only SELECT or SELECT with a CTE.")
    statement = statements[0]
    _validate_expressions(statement, dialect)
    _scope_relations(statement, dialect, schema, tables)
    _cap_rows(statement, row_limit)

    try:
        return statement.sql(
            dialect=sqlglot_dialect, comments=False, unsupported_level=ErrorLevel.RAISE
        )
    except (SqlglotError, RecursionError):
        raise QuerySyntaxError(f"The query uses syntax unsupported by {dialect}.") from None


def _looks_like_single_read_query(sql: str, dialect: str) -> bool:
    """Distinguish obvious writes or extra statements from fixable SELECT syntax."""
    try:
        tokens = [token.token_type for token in sqlglot.tokenize(sql, read=dialect)]
    except SqlglotError:
        return False
    if not tokens or tokens[0] not in {TokenType.SELECT, TokenType.WITH}:
        return False
    while tokens and tokens[-1] == TokenType.SEMICOLON:
        tokens.pop()
    return TokenType.SEMICOLON not in tokens and _WRITE_TOKENS.isdisjoint(tokens)


def _validate_expressions(statement: exp.Expression, dialect: str) -> None:
    for node in statement.walk():
        if isinstance(node, _BLOCKED_NODES):
            raise UnsafeQueryError("The query contains an operation outside read-only SELECT.")
        if isinstance(node, exp.DataType) and node.this == exp.DataType.Type.USERDEFINED:
            raise UnsafeQueryError("Casts must use built-in database types.")
        if isinstance(node, exp.Dot) and isinstance(node.expression, exp.Func):
            raise UnsafeQueryError("Call only unqualified built-in analytics functions.")
        if isinstance(node, exp.Func) and not isinstance(node, (exp.And, exp.Or, exp.Case)):
            name = node.name if isinstance(node, exp.Anonymous) else node.sql_name()
            name = name.upper()
            if name not in _COMMON_FUNCTIONS and name not in _DIALECT_FUNCTIONS[dialect]:
                raise UnsafeQueryError(f"Function {name} is outside the supported analytics functions.")
            if isinstance(node, exp.Anonymous) and isinstance(node.this, exp.Identifier):
                if node.this.args.get("quoted"):
                    raise UnsafeQueryError("Call built-in analytics functions without quotes.")


def _identifier_name(identifier: exp.Identifier, dialect: str) -> str:
    if dialect == "sqlite" or (dialect == "postgresql" and not identifier.args.get("quoted")):
        return identifier.name.translate(_ASCII_LOWER)
    return identifier.name


def _scope_relations(
    statement: exp.Expression, dialect: str, schema: str, tables: Collection[str]
) -> None:
    # Apply each engine's CTE-name matching rules without altering output aliases.
    identifiers = [(node, node.name) for node in statement.find_all(exp.Identifier)]
    try:
        for node, _name in identifiers:
            node.set("this", _identifier_name(node, dialect))
        cte_refs = {
            id(reference)
            for scope in traverse_scope(statement)
            for reference, source in scope.selected_sources.values()
            if isinstance(reference, exp.Table)
            and isinstance(source, Scope)
            and not reference.db
            and not reference.catalog
        }
    except SqlglotError:
        raise QuerySyntaxError("Check table references and give repeated sources distinct aliases.") from None
    finally:
        for node, name in identifiers:
            node.set("this", name)

    expected_schema = schema.translate(_ASCII_LOWER) if dialect == "sqlite" else schema
    known_tables = {name.translate(_ASCII_LOWER) if dialect == "sqlite" else name for name in tables}
    for table in statement.find_all(exp.Table):
        if id(table) in cte_refs:
            continue
        if not isinstance(table.this, exp.Identifier):
            raise UnsafeQueryError("Use a discovered table or view as each named source.")
        qualifier = table.args.get("db")
        if table.catalog or (
            qualifier and _identifier_name(qualifier, dialect) != expected_schema
        ):
            raise UnsafeQueryError(f"Query only tables and views from the {schema} schema.")
        if _identifier_name(table.this, dialect) not in known_tables:
            raise UnknownRelationError(f"Table or view {table.name} was not found in the selected schema.")
        # Bind to the exact schema even when a temporary or system object shadows a name.
        table.set("db", exp.to_identifier(schema, quoted=True))


def _cap_rows(statement: exp.Expression, row_limit: int) -> None:
    """Preserve smaller explicit limits and fetch one extra row to detect truncation."""
    if row_limit < 1:
        raise ValueError("A positive result row limit is required.")
    cap = row_limit + 1
    limit = statement.args.get("limit")
    if limit is not None:
        options = limit.args.get("limit_options")
        if options and (options.args.get("percent") or options.args.get("with_ties")):
            raise UnsafeQueryError("Use a fixed LIMIT without percentages or WITH TIES.")
        count = limit.args.get("count") if isinstance(limit, exp.Fetch) else limit.expression
        if count is None and isinstance(limit, exp.Fetch):
            cap = 1
        elif isinstance(count, exp.Literal) and count.is_int and count.this.isascii() and count.this.isdigit():
            digits = count.this.lstrip("0")
            if len(digits) <= len(str(cap)):
                cap = min(cap, int(digits or "0"))
        elif not (isinstance(count, exp.Var) and count.name.upper() == "ALL"):
            raise UnsafeQueryError("Use a non-negative whole number in the outer LIMIT.")
    statement.set("limit", exp.Limit(expression=exp.Literal.number(cap)))
