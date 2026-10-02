"""Exécuteur d'ACCOUNTING_PROMPT (nouveau système Router/Tools).

Construit le prompt par remplacement de texte (jamais PromptTemplate/.format(),
même raison que RouterExecutor : accolades JSON littérales dans les exemples
du prompt), puis délègue l'appel LLM à StructuredLLMExecutor (base_executor.py).

Utilise AccountingExtractionLLMResult (amount_ttc en float) comme cible de
with_structured_output() — AccountingExtractionResult (Decimal) fait échouer
la conversion en grammaire GBNF côté Ollama (voir schemas/accounting.py pour
le détail du bug et sa correction). Le résultat LLM est converti vers le
schéma interne (Decimal) juste après réception, via .from_llm_result().
"""

import logging

from apps.femi_agent.agent.base_executor import BaseAgentExecutionError, StructuredLLMExecutor
from apps.femi_agent.agent.prompts.accounting_prompt import ACCOUNTING_PROMPT
from apps.femi_agent.schemas import AccountingExtractionLLMResult, AccountingExtractionResult
from apps.femi_agent.agent.tools.categories import get_categories_disponibles

logger = logging.getLogger(__name__)

# Le prompt système ACCOUNTING (~855 lignes, nombreux exemples JSON) dépasse
# largement le contexte par défaut d'Ollama (2048) — même précaution que pour
# ROUTER_PROMPT (voir router_executor.py), sans toucher au défaut partagé par
# l'ancien pipeline (voir llm.py).
ACCOUNTING_NUM_CTX = 8192

# Point 6 : injection dynamique des catégories du tenant via
# get_categories_disponibles(entreprise), avec repli statique si le tenant
# n'a aucune Categorie déclarée ou si aucun categories_disponibles fourni.
DEFAULT_CATEGORIES = "(catégories non disponibles)"


# Contexte entreprise + règles de sens pour les DOCUMENTS (factures, reçus,
# tickets) issus de l'OCR. Sans le nom de l'entreprise, le modèle ne peut pas
# savoir si un document est une vente (l'entreprise est l'émetteur) ou un
# achat (l'entreprise est le client) : il devinait. Placé en tête du prompt.
# Aucune accolade dans ce texte : le nom est injecté par .replace().
_ENTREPRISE_CONTEXT_TEMPLATE = """==================================================
CONTEXTE ENTREPRISE ET SENS DES DOCUMENTS (PRIORITAIRE)
==================================================

L'entreprise de l'utilisateur s'appelle : __NOM_ENTREPRISE__

Lorsque le texte provient d'un document (facture, reçu, ticket de caisse,
bon de commande) et que l'utilisateur n'a pas précisé le sens de
l'opération, détermine-le ainsi :

1. Si __NOM_ENTREPRISE__ (ou une variante évidente de ce nom) apparaît comme
   émetteur, vendeur ou en-tête du document
   → l'entreprise a VENDU : transaction_type = RECETTE.

2. Si __NOM_ENTREPRISE__ apparaît comme client, destinataire, "facturé à",
   "doit" ou "client :"
   → l'entreprise a ACHETÉ : transaction_type = DEPENSE.

3. Un ticket de caisse ou un reçu émis par un commerçant tiers, sans aucune
   mention de __NOM_ENTREPRISE__, est un achat : transaction_type = DEPENSE.

4. Si le nom de l'entreprise n'apparaît nulle part ET que le document ne
   permet pas de savoir qui vend et qui achète
   → needs_clarification = true, missing_fields contient
   "transaction_type". Ne devine JAMAIS le sens.

5. Ce que l'utilisateur écrit explicitement dans son message ("j'ai vendu",
   "j'ai acheté") prime toujours sur ces règles.

Statut de paiement d'un document : statut_paiement = CREDIT uniquement si
le document ou le message l'indique explicitement (ex : "à crédit",
"reste à payer", "solde dû", "non réglé", "échéance", acompte avec solde
restant). Sinon, applique les règles habituelles ci-dessous.

"""


_DOCUMENT_PAIEMENT_RULE = """==================================================
STATUT DE PAIEMENT D'UN DOCUMENT (PRIORITAIRE)
==================================================

Le texte à analyser provient d'un DOCUMENT envoyé en photo ou en fichier
(et non d'une simple phrase dictée). Pour chaque opération issue de ce
document, détermine le statut de paiement ainsi :

1. Ticket de caisse, reçu de paiement, ticket de terminal de paiement :
   payé par nature → statut_paiement = PAYE. Ne pose AUCUNE question.

2. Facture, bon de commande, devis, bon de livraison ou tout autre
   document commercial :
   a. paiement indiqué comme effectué ("payé", "acquitté", "réglé",
      "payé par MoMo / espèces / virement / chèque", cachet PAYÉ)
      → statut_paiement = PAYE.
   b. crédit ou solde indiqué ("à crédit", "reste à payer", "solde dû",
      "non réglé", "échéance", acompte avec solde restant)
      → statut_paiement = CREDIT.
   c. AUCUNE mention de paiement → ne devine JAMAIS :
      needs_clarification = true et missing_fields contient
      "statut_paiement" (en plus des autres champs manquants éventuels).
      Renseigne quand même tous les autres champs que tu as pu extraire.

3. Ce que l'utilisateur écrit explicitement dans son message ("c'est payé",
   "à crédit") prime toujours sur le document.

"""


_PAIEMENT_PARTIEL_RULE = """==================================================
AVANCE, ACOMPTE ET RESTE À PAYER (PRIORITAIRE)
==================================================

Lis TOUT le texte du document ou du message, y compris le bas de page :
"acompte", "avance", "versé", "déjà payé", "reste à payer", "solde",
"net à payer".

Si une partie seulement a été payée :

→ amount_ttc = le MONTANT TOTAL de la facture (jamais le reste à payer) ;
→ montant_deja_paye = la somme déjà versée (avance / acompte) ;
→ statut_paiement = CREDIT.

Exemple : total 2 100 000, avance 1 700 000, reste 400 000
→ amount_ttc = 2100000, montant_deja_paye = 1700000, statut_paiement = CREDIT.

Si tout est payé : statut_paiement = PAYE, montant_deja_paye = null.
Si rien n'est payé : statut_paiement = CREDIT, montant_deja_paye = null.
Si le document donne l'avance et le reste, ne pose AUCUNE question sur le
paiement : tout est déjà connu.
Ne calcule jamais toi-même une avance qui n'est pas écrite.

"""


def _build_entreprise_context(nom_entreprise: str | None) -> str:
    nom = (nom_entreprise or "").strip() or "(nom non renseigné)"
    return _ENTREPRISE_CONTEXT_TEMPLATE.replace("__NOM_ENTREPRISE__", nom)


class AccountingExecutionError(BaseAgentExecutionError):
    """Exception personnalisée encapsulant les échecs d'exécution de l'agent ACCOUNTING."""
    pass


class AccountingExecutor:
    """
    Exécuteur d'ACCOUNTING_PROMPT : construit le prompt par remplacement de
    texte, appelle le LLM avec sortie structurée contrainte au schéma
    AccountingExtractionLLMResult (float), puis convertit le résultat vers
    AccountingExtractionResult (Decimal) avant de le retourner.
    """

    @staticmethod
    def _build_prompt_text(
        categories_disponibles: str,
        nom_entreprise: str | None = None,
        from_document: bool = False,
    ) -> str:
        """Injecte les variables d'ACCOUNTING_PROMPT dans le texte brut du prompt.

        Le bloc CONTEXTE ENTREPRISE (nom + règles de sens des documents) est
        ajouté ici, à l'exécution, et non dans ACCOUNTING_PROMPT lui-même :
        l'ancien pipeline (executor.py) charge ce prompt via PromptTemplate,
        qui casserait sur un nouveau placeholder.
        """
        base = ACCOUNTING_PROMPT.replace("{categories_disponibles}", categories_disponibles)
        document_rule = _DOCUMENT_PAIEMENT_RULE if from_document else ""
        return (
            _build_entreprise_context(nom_entreprise)
            + _PAIEMENT_PARTIEL_RULE
            + document_rule
            + base
        )

    @classmethod
    def execute(
        cls,
        message_text: str,
        entreprise,
        categories_disponibles: str | None = None,
        from_document: bool = False,
    ) -> AccountingExtractionResult:
        """Exécution synchrone de l'agent ACCOUNTING.

        Args:
            message_text: Message utilisateur (segment routé vers ACCOUNTING par le Router).
            entreprise: Instance Entreprise du tenant (utilisée pour résoudre les catégories réelles).
            categories_disponibles: Liste des catégories du tenant, déjà formatée en texte.
                Optionnel — si absent, résolu dynamiquement via get_categories_disponibles(entreprise).
            from_document: True si le texte provient d'une photo/fichier (OCR). Active la
                règle de clarification du statut de paiement pour les factures sans mention.

        Returns:
            AccountingExtractionResult (schéma interne, amount_ttc en Decimal).
        """
        prompt_text = cls._build_prompt_text(
            categories_disponibles or get_categories_disponibles(entreprise) or DEFAULT_CATEGORIES,
            getattr(entreprise, "nom", None),
            from_document,
        )
        llm_result: AccountingExtractionLLMResult = StructuredLLMExecutor.execute(
            prompt_text=prompt_text,
            message_text=message_text,
            output_schema=AccountingExtractionLLMResult,
            num_ctx=ACCOUNTING_NUM_CTX,
            error_cls=AccountingExecutionError,
            log_prefix="AccountingExecutor",
        )
        return AccountingExtractionResult.from_llm_result(llm_result)

    @classmethod
    async def aexecute(
        cls,
        message_text: str,
        entreprise,
        categories_disponibles: str | None = None,
        from_document: bool = False,
    ) -> AccountingExtractionResult:
        """Exécution asynchrone de l'agent ACCOUNTING (mêmes arguments que execute())."""
        prompt_text = cls._build_prompt_text(
            categories_disponibles or get_categories_disponibles(entreprise) or DEFAULT_CATEGORIES,
            getattr(entreprise, "nom", None),
            from_document,
        )
        llm_result: AccountingExtractionLLMResult = await StructuredLLMExecutor.aexecute(
            prompt_text=prompt_text,
            message_text=message_text,
            output_schema=AccountingExtractionLLMResult,
            num_ctx=ACCOUNTING_NUM_CTX,
            error_cls=AccountingExecutionError,
            log_prefix="AccountingExecutor",
        )
        return AccountingExtractionResult.from_llm_result(llm_result)