"""
Schémas Pydantic pour la sortie d'ACCOUNTING_PROMPT (nouveau système
Router/Tools, en construction — coexiste avec ollama_gbnf_extraction.py
(ancien pipeline) pendant la transition.

Correspond EXACTEMENT au format JSON défini en section 17 d'accounting_prompt.py :

    {
      "transactions": [
        {
          "transaction_type": "RECETTE|DEPENSE|PRET_DONNE|PRET_RECU",
          "amount_ttc": 0,
          "currency": "XOF",
          "category": null,
          "payment_method": null,
          "contact": null,
          "date_operation": null,
          "description": "",
          "check_open_debt": false,
          "check_open_loan": false,
          "confidence": "high|medium|low"
        }
      ],
      "needs_clarification": false,
      "missing_fields": []
    }

Ne PAS réutiliser PaymentMethodEnum de ollama_gbnf_extraction.py : les
valeurs sont incompatibles (CASH/MOBILE_MONEY/BANK_TRANSFER/CARD/OTHER vs
CASH/TMONEY/FLOOZ/VIREMENT/CARTE, spécifique Togo/mobile money local).

NOTE (Sept 15, 2026) : le champ était initialement nommé `date`, ce qui
masquait le type importé `datetime.date` lors de l'évaluation du corps de
classe et produisait un JSON Schema cassé (`"type": "null"` seul, sans
`anyOf`). Renommé en `date_operation` — bug réel, mais indépendant du
problème ci-dessous.

NOTE (Sept 15, 2026) : `amount_ttc` en `Decimal` fait échouer la conversion
JSON Schema -> grammaire GBNF côté Ollama (`Failed to initialize samplers:
failed to parse grammar`), confirmé par bissection sur des modèles Pydantic
minimaux testés en conditions réelles (with_structured_output). `float`
fonctionne. Mais changer juste l'annotation du champ en `float` tout en
gardant un validator "before" qui retourne un Decimal ne suffit pas :
Pydantic re-coerce la valeur du validator contre l'annotation du champ
juste après, donc le Decimal serait silencieusement réécrasé en float.

Solution : deux schémas séparés.
- AccountingTransactionLLMSchema / AccountingExtractionLLMResult :
  UNIQUEMENT utilisés comme cible de with_structured_output() (amount_ttc
  en float — seul type qu'Ollama sait convertir en grammaire ici).
- AccountingTransactionSchema / AccountingExtractionResult : utilisés par
  le reste du système (amount_ttc en Decimal, pour la précision). Convertis
  à partir des schémas LLM juste après réception de la réponse du modèle
  (voir AccountingExecutor, qui fait cette conversion).
"""

from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any, Literal, Optional
from .common import ConfidenceEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AccountingPaymentMethodEnum(str, Enum):
    """Modes de paiement — vocabulaire d'accounting_prompt.py section 6."""
    CASH = "CASH"
    TMONEY = "TMONEY"
    FLOOZ = "FLOOZ"
    VIREMENT = "VIREMENT"
    CARTE = "CARTE"


# Alias conservé pour compatibilité — la définition vit maintenant dans
# common.py (ConfidenceEnum), vocabulaire partagé par tous les agents.
AccountingConfidenceEnum = ConfidenceEnum

# --- SCHÉMAS "LLM" (cible directe de with_structured_output()) ---

class AccountingTransactionLLMSchema(BaseModel):
    """
    Une transaction telle que produite DIRECTEMENT par le LLM
    (ACCOUNTING_PROMPT).

    Les montants sont en float ici car ce schéma est utilisé directement
    par with_structured_output() / Ollama.

    Ne pas utiliser ce schéma pour la persistance en base :
    utiliser AccountingTransactionSchema.
    """

    model_config = ConfigDict(extra="ignore")

    transaction_type: Literal[
        "RECETTE",
        "DEPENSE",
        "PRET_DONNE",
        "PRET_RECU",
    ]

    amount_ttc: Optional[float] = Field(default=None, ge=0)
    amount_ht: Optional[float] = Field(default=None, ge=0)
    tax_amount: Optional[float] = Field(default=None, ge=0)
    # Avance / acompte déjà versé (facture partiellement payée). Null si aucun.
    montant_deja_paye: Optional[float] = Field(default=None, ge=0)

    currency: Optional[str] = Field(default=None, max_length=5)
    category: Optional[str] = None
    payment_method: Optional[AccountingPaymentMethodEnum] = None
    contact: Optional[str] = None
    date_operation: Optional[date] = None
    description: str = ""

    statut_paiement: Literal["PAYE", "CREDIT"] = "PAYE"

    check_open_debt: bool = False
    check_open_loan: bool = False

    confidence: AccountingConfidenceEnum = AccountingConfidenceEnum.MEDIUM

    @field_validator("date_operation", mode="before")
    @classmethod
    def parse_date_operation(cls, value: Any) -> Optional[date]:
        """Le prompt garantit YYYY-MM-DD ou null."""
        if value is None or value == "":
            return None
        return value
    
        # NOUVEAU
    statut_paiement: Literal["PAYE", "CREDIT"] = "PAYE"

    check_open_debt: bool = False
    check_open_loan: bool = False

    confidence: AccountingConfidenceEnum = AccountingConfidenceEnum.MEDIUM


class AccountingExtractionLLMResult(BaseModel):
    """Enveloppe complète telle que produite DIRECTEMENT par le LLM (section 17).
    Cible de with_structured_output() dans AccountingExecutor. Convertie en
    AccountingExtractionResult juste après réception."""

    model_config = ConfigDict(extra="ignore")

    transactions: list[AccountingTransactionLLMSchema] = Field(default_factory=list)
    needs_clarification: bool = False
    missing_fields: list[str] = Field(default_factory=list)

    @field_validator("transactions")
    @classmethod
    def no_double_check_flags(cls, transactions):
        for t in transactions:
            if t.check_open_debt and t.check_open_loan:
                raise ValueError(
                    "check_open_debt et check_open_loan ne peuvent pas être "
                    "true simultanément pour une même transaction (règle "
                    "accounting_prompt.py section 12)."
                )
        return transactions


# --- SCHÉMAS INTERNES (utilisés par le reste du système, amount_ttc en Decimal) ---

class AccountingTransactionSchema(BaseModel):
    """
    Une transaction individuelle, pour usage interne.

    Les montants sont en Decimal pour conserver la précision financière.

    Ce schéma est obtenu par conversion depuis
    AccountingTransactionLLMSchema et ne doit jamais être passé
    directement à with_structured_output().
    """

    model_config = ConfigDict(extra="ignore")

    transaction_type: Literal[
        "RECETTE",
        "DEPENSE",
        "PRET_DONNE",
        "PRET_RECU",
    ]

    amount_ttc: Optional[Decimal] = Field(default=None, ge=0)
    amount_ht: Optional[Decimal] = Field(default=None, ge=0)
    tax_amount: Optional[Decimal] = Field(default=None, ge=0)
    montant_deja_paye: Optional[Decimal] = Field(default=None, ge=0)

    currency: Optional[str] = Field(default=None, max_length=5)
    category: Optional[str] = None
    payment_method: Optional[AccountingPaymentMethodEnum] = None
    contact: Optional[str] = None
    date_operation: Optional[date] = None
    description: str = ""

    statut_paiement: Literal["PAYE", "CREDIT"] = "PAYE"

    check_open_debt: bool = False
    check_open_loan: bool = False

    confidence: AccountingConfidenceEnum = AccountingConfidenceEnum.MEDIUM

    @field_validator(
        "amount_ttc",
        "amount_ht",
        "tax_amount",
        "montant_deja_paye",
        mode="before",
    )
    @classmethod
    def parse_amount(cls, value: Any) -> Optional[Decimal]:
        """
        Convertit proprement les floats venant du LLM en Decimal
        sans subir les erreurs d'arrondi binaire des floats.
        """
        if value is None:
            return None

        if isinstance(value, Decimal):
            return value

        return Decimal(str(value))

    @classmethod
    def from_llm_schema(
        cls,
        llm_schema: AccountingTransactionLLMSchema,
    ) -> "AccountingTransactionSchema":
        """Convertit une sortie LLM (float) en schéma interne (Decimal)."""
        return cls(**llm_schema.model_dump())


class AccountingExtractionResult(BaseModel):
    """Enveloppe complète, pour usage interne (Decimal). Obtenue par conversion
    depuis AccountingExtractionLLMResult."""

    model_config = ConfigDict(extra="ignore")

    transactions: list[AccountingTransactionSchema] = Field(default_factory=list)
    needs_clarification: bool = False
    missing_fields: list[str] = Field(default_factory=list)

    @field_validator("transactions")
    @classmethod
    def no_double_check_flags(cls, transactions):
        for t in transactions:
            if t.check_open_debt and t.check_open_loan:
                raise ValueError(
                    "check_open_debt et check_open_loan ne peuvent pas être "
                    "true simultanément pour une même transaction (règle "
                    "accounting_prompt.py section 12)."
                )
        return transactions

    @classmethod
    def from_llm_result(cls, llm_result: AccountingExtractionLLMResult) -> "AccountingExtractionResult":
        """Convertit le résultat brut du LLM (float) en résultat interne (Decimal)."""
        return cls(
            transactions=[
                AccountingTransactionSchema.from_llm_schema(t) for t in llm_result.transactions
            ],
            needs_clarification=llm_result.needs_clarification,
            missing_fields=llm_result.missing_fields,
        )