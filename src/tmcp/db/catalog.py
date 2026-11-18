"""The data catalog: datasets, tables and columns that the server knows.

Two datasets exist (OP-01):
- ``reference``: the original titanic.db, read-only SQLite in the image.
- ``working``: a writable PostgreSQL copy of the view ``passengers``. Changes need an approval.
"""

from dataclasses import dataclass, field
from typing import Literal

Dataset = Literal["reference", "working"]
DATASETS: tuple[Dataset, ...] = ("reference", "working")


@dataclass(frozen=True)
class Column:
    name: str
    type: Literal["integer", "real", "text"]
    description: str
    nullable: bool = False
    values: tuple[str | int, ...] | None = None


@dataclass(frozen=True)
class Table:
    name: str
    description: str
    columns: tuple[Column, ...]
    is_view: bool = False
    column_names: frozenset[str] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "column_names", frozenset(c.name for c in self.columns))

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name), None)


PASSENGER_COLUMNS: tuple[Column, ...] = (
    Column("passenger_id", "integer", "Stable row identifier (SQLite rowid of Observation)."),
    Column("survived", "integer", "1 = the passenger survived, 0 = the passenger died.", values=(0, 1)),
    Column("pclass", "integer", "Ticket class as a number.", values=(1, 2, 3)),
    Column("class", "text", "Ticket class as text.", values=("First", "Second", "Third")),
    Column("sex", "text", "Sex of the passenger.", values=("female", "male")),
    Column("age", "real", "Age in years. NULL if unknown (177 rows in the reference data).", nullable=True),
    Column("sibsp", "integer", "Number of siblings and spouses on board."),
    Column("parch", "integer", "Number of parents and children on board."),
    Column("fare", "real", "Ticket price in pounds.", nullable=True),
    Column("embarked", "text", "Port code. NULL if unknown.", nullable=True, values=("C", "Q", "S")),
    Column(
        "embark_town",
        "text",
        "Port name. NULL if unknown.",
        nullable=True,
        values=("Cherbourg", "Queenstown", "Southampton"),
    ),
    Column("deck", "text", "Deck letter. NULL if unknown (688 rows).", nullable=True, values=tuple("ABCDEFG")),
    Column("who", "text", "Category of person.", values=("child", "man", "woman")),
    Column("adult_male", "integer", "1 = adult male.", values=(0, 1)),
    Column("alone", "integer", "1 = travelled without family.", values=(0, 1)),
    Column("alive", "text", "Survival as text.", values=("no", "yes")),
)

PASSENGERS_VIEW = Table(
    "passengers",
    "One row for each passenger. Joins Observation with all lookup tables and shows text values. "
    "Use this table for most questions.",
    PASSENGER_COLUMNS,
    is_view=True,
)


def _lookup(name: str, id_col: str, value_col: str, description: str) -> Table:
    return Table(
        name,
        description,
        (
            Column(id_col, "integer", "Identifier. -1 means unknown."),
            Column(value_col, "text", "Value text."),
        ),
    )


OBSERVATION = Table(
    "Observation",
    "Raw passenger facts with foreign key IDs into the lookup tables. -1 means unknown.",
    (
        Column("survived", "integer", "1 = survived, 0 = died."),
        Column("pclass", "integer", "Ticket class 1, 2 or 3."),
        Column("age", "real", "Age in years.", nullable=True),
        Column("sibsp", "integer", "Siblings and spouses on board."),
        Column("parch", "integer", "Parents and children on board."),
        Column("fare", "real", "Ticket price.", nullable=True),
        Column("adult_male", "integer", "1 = adult male."),
        Column("alone", "integer", "1 = travelled alone."),
        Column("sex_id", "integer", "Foreign key to Sex."),
        Column("embarked_id", "integer", "Foreign key to Embarked."),
        Column("class_id", "integer", "Foreign key to Class."),
        Column("who_id", "integer", "Foreign key to Who."),
        Column("deck_id", "integer", "Foreign key to Deck."),
        Column("embark_town_id", "integer", "Foreign key to EmbarkTown."),
        Column("alive_id", "integer", "Foreign key to Alive."),
    ),
)

REFERENCE_TABLES: dict[str, Table] = {
    t.name.lower(): t
    for t in (
        PASSENGERS_VIEW,
        OBSERVATION,
        _lookup("Sex", "sex_id", "sex", "Lookup: sex."),
        _lookup("Class", "class_id", "class", "Lookup: ticket class."),
        _lookup("Who", "who_id", "who", "Lookup: person category."),
        _lookup("Deck", "deck_id", "deck", "Lookup: deck letter."),
        _lookup("Embarked", "embarked_id", "embarked", "Lookup: port code."),
        _lookup("EmbarkTown", "embark_town_id", "embark_town", "Lookup: port name."),
        _lookup("Alive", "alive_id", "alive", "Lookup: survival text."),
    )
}

WORKING_TABLES: dict[str, Table] = {
    "passengers": Table(
        "passengers",
        "Writable working copy of the passengers view (PostgreSQL). Changes go through change requests "
        "and need the approval of a second agent.",
        PASSENGER_COLUMNS,
    )
}

CATALOG: dict[Dataset, dict[str, Table]] = {"reference": REFERENCE_TABLES, "working": WORKING_TABLES}


def get_table(dataset: Dataset, name: str) -> Table | None:
    return CATALOG[dataset].get(name.lower())


# DS-03 / DS-04: the view joins all lookup tables and shows NULL in place of the code -1.
REFERENCE_VIEW_SQL = """
CREATE TEMP VIEW passengers AS
SELECT
    o.rowid          AS passenger_id,
    o.survived       AS survived,
    o.pclass         AS pclass,
    c.class          AS class,
    s.sex            AS sex,
    o.age            AS age,
    o.sibsp          AS sibsp,
    o.parch          AS parch,
    o.fare           AS fare,
    e.embarked       AS embarked,
    et.embark_town   AS embark_town,
    d.deck           AS deck,
    w.who            AS who,
    o.adult_male     AS adult_male,
    o.alone          AS alone,
    a.alive          AS alive
FROM main.Observation AS o
LEFT JOIN main.Class      AS c  ON c.class_id = o.class_id AND o.class_id <> -1
LEFT JOIN main.Sex        AS s  ON s.sex_id = o.sex_id AND o.sex_id <> -1
LEFT JOIN main.Embarked   AS e  ON e.embarked_id = o.embarked_id AND o.embarked_id <> -1
LEFT JOIN main.EmbarkTown AS et ON et.embark_town_id = o.embark_town_id AND o.embark_town_id <> -1
LEFT JOIN main.Deck       AS d  ON d.deck_id = o.deck_id AND o.deck_id <> -1
LEFT JOIN main.Who        AS w  ON w.who_id = o.who_id AND o.who_id <> -1
LEFT JOIN main.Alive      AS a  ON a.alive_id = o.alive_id AND o.alive_id <> -1
"""
