"""
Point d'entrée du nouveau pipeline (Router + agents spécialisés).

Pipeline :

Utilisateur
    ↓
FemiRouterManager
    ↓
OCR / STT si nécessaire
    ↓
RouterExecutor
    ↓
Agent spécialisé
    ↓
Validation / extraction
    ↓
Persistance éventuelle
    ↓
RouterProcessResult
"""

import logging
from typing import Optional, Tuple

from asgiref.sync import sync_to_async

from apps.femi_account.models import (
    Conversation,
    Entreprise,
    Operation,
    PieceJustificative,
    Utilisateur,
)

from apps.femi_agent.agent.accounting_executor import AccountingExecutor
from apps.femi_agent.agent.customer_executor import CustomerExecutor
from apps.femi_agent.agent.financial_analyst_executor import (
    FinancialAnalystExecutor,
)
from apps.femi_agent.agent.accounting_modify_executor import (
    AccountingModifyExecutor,
)

from apps.femi_agent.agent.accounting_manager import (
    save_accounting_transactions,
)
from apps.femi_agent.agent.conversation_manager import (
    build_history_text,
    get_or_create_active_conversation,
    normalize_canal,
    record_message,
    resolve_type_message,
)
from apps.femi_agent.agent.pending_action import PendingTurn
from apps.femi_agent.agent.social_replies import (
    build_off_topic_reply,
    build_social_reply,
    classify_social_message,
)
from django.utils import timezone

from apps.femi_account.integrations.supabase_storage import upload_file

from apps.femi_agent.agent.ocr_executor import (
    OcrExecutor,
    OcrExecutionError,
)

from apps.femi_agent.agent.stt_executor import (
    SttExecutor,
    SttExecutionError,
)

from apps.femi_agent.agent.router_executor import (
    RouterExecutor,
    RouterExecutionError,
)

from apps.femi_agent.parsers.audio_parser import transcribe_audio

from apps.femi_agent.schemas import (
    AccountingExtractionResult,
    AccountingModifyResolutionResult,
    AccountingModifyProposeChangeResult,
    AccountingModifySearchResult,
    CustomerExtractionOutput,
    FinalAnswerOutput,
    RouterIntent,
    RouterOutput,
    RouterProcessResult,
    ToolSelectionOutput,
    ValidationOutput,
)


logger = logging.getLogger(__name__)


DispatchResult = Optional[
    AccountingExtractionResult
    | ToolSelectionOutput
    | FinalAnswerOutput
    | AccountingModifySearchResult
    | AccountingModifyResolutionResult
    | AccountingModifyProposeChangeResult
]


class RouterInputError(Exception):
    """
    Aucune donnée textuelle ou multimédia exploitable n'a été fournie.
    """

    pass


class FemiRouterManager:
    """
    Gestionnaire central du nouveau pipeline :

        Router
            ↓
        Agent spécialisé
            ↓
        Persistance éventuelle
    """

    # ============================================================
    # POINT D'ENTRÉE ASYNCHRONE
    # ============================================================

    @classmethod
    async def aroute_message(
        cls,
        message_text: Optional[str] = None,
        entreprise_id: Optional[str] = None,
        utilisateur_id: Optional[str] = None,
        source: str = "API",
        image_bytes: Optional[bytes] = None,
        audio_bytes: Optional[bytes] = None,
    ) -> RouterProcessResult:

        try:

            # ----------------------------------------------------
            # 1. Résolution du tenant et de l'utilisateur
            # ----------------------------------------------------

            entreprise, utilisateur = await sync_to_async(
                cls._get_tenant_context_stricte
            )(
                entreprise_id,
                utilisateur_id,
            )

            # ----------------------------------------------------
            # 1bis. Contexte conversationnel (historique multi-canal,
            # best-effort — voir conversation_manager.py)
            # ----------------------------------------------------

            conversation, canal, type_message, history_text = (
                await sync_to_async(
                    cls._setup_conversation_context
                )(
                    utilisateur,
                    entreprise,
                    source,
                    image_bytes,
                    audio_bytes,
                )
            )

            # ----------------------------------------------------
            # 1ter. Court-circuit salutation (voir route_message)
            # ----------------------------------------------------

            if (
                message_text
                and not image_bytes
                and not audio_bytes
                and cls._is_pure_greeting(message_text.strip())
            ):
                greeting_result = cls._greeting_result(message_text.strip())

                await cls._arecord_turn(
                    conversation,
                    canal,
                    message_text.strip(),
                    type_message,
                    greeting_result.message,
                )

                return greeting_result

            # ----------------------------------------------------
            # 2. Construction du texte final
            # ----------------------------------------------------

            combined_text = await cls._abuild_combined_text(
                message_text,
                image_bytes,
                audio_bytes,
            )

            # ----------------------------------------------------
            # 3. Routage
            # ----------------------------------------------------

            # Actions en attente de clarification (best-effort)
            pending = await sync_to_async(PendingTurn.load)(
                conversation
            )

            router_output = await RouterExecutor.aexecute(
                combined_text,
                history=history_text,
                **pending.router_kwargs(),
            )

            pending.start_turn(
                combined_text,
                len(router_output.intents),
            )

            # ----------------------------------------------------
            # 4. Dispatch + persistance
            # ----------------------------------------------------

            result = await cls._abuild_result(
                router_output=router_output,
                entreprise=entreprise,
                utilisateur=utilisateur,
                source=source,
                raw_text=combined_text,
                image_bytes=image_bytes,
                pending=pending,
            )

            await sync_to_async(pending.commit)()

            await cls._arecord_turn(
                conversation,
                canal,
                combined_text,
                type_message,
                result.message,
            )

            return result

        except (
            Entreprise.DoesNotExist,
            Utilisateur.DoesNotExist,
        ) as exc:

            logger.error(
                "[FemiRouterManager] "
                "Contexte tenant introuvable (async): %s",
                exc,
            )

            return RouterProcessResult(
                success=False,
                message=str(exc),
            )

        except RouterInputError as exc:

            logger.warning(
                "[FemiRouterManager] "
                "Aucune donnée exploitable (async): %s",
                exc,
            )

            return RouterProcessResult(
                success=False,
                message=str(exc),
            )

        except (
            OcrExecutionError,
            SttExecutionError,
            ValueError,
        ) as exc:

            logger.error(
                "[FemiRouterManager] "
                "Échec OCR/audio (async): %s",
                exc,
            )

            return RouterProcessResult(
                success=False,
                message=(
                    "Le traitement de l'image ou de l'audio "
                    "a échoué."
                ),
            )

        except RouterExecutionError as exc:

            logger.error(
                "[FemiRouterManager] "
                "Échec du Router (async): %s",
                exc,
            )

            return RouterProcessResult(
                success=False,
                message="Le routage du message a échoué.",
            )

        except Exception:

            logger.exception(
                "[FemiRouterManager] "
                "Erreur inattendue (async)"
            )

            return RouterProcessResult(
                success=False,
                message="Une erreur technique est survenue.",
            )

    # ============================================================
    # POINT D'ENTRÉE SYNCHRONE
    # ============================================================

    @classmethod
    def route_message(
        cls,
        message_text: Optional[str] = None,
        entreprise_id: Optional[str] = None,
        utilisateur_id: Optional[str] = None,
        source: str = "API",
        image_bytes: Optional[bytes] = None,
        audio_bytes: Optional[bytes] = None,
    ) -> RouterProcessResult:

        try:

            # ----------------------------------------------------
            # 1. Résolution tenant + utilisateur
            # ----------------------------------------------------

            entreprise, utilisateur = (
                cls._get_tenant_context_stricte(
                    entreprise_id,
                    utilisateur_id,
                )
            )

            # ----------------------------------------------------
            # 1bis. Contexte conversationnel (historique multi-canal,
            # best-effort — voir conversation_manager.py)
            # ----------------------------------------------------

            conversation, canal, type_message, history_text = (
                cls._setup_conversation_context(
                    utilisateur,
                    entreprise,
                    source,
                    image_bytes,
                    audio_bytes,
                )
            )

            # ----------------------------------------------------
            # 1ter. Court-circuit salutation (pas d'image/audio,
            # message très court type "Salut Femi") — évite un
            # appel LLM inutile et répond immédiatement.
            # ----------------------------------------------------

            if (
                message_text
                and not image_bytes
                and not audio_bytes
                and cls._is_pure_greeting(message_text.strip())
            ):
                greeting_result = cls._greeting_result(message_text.strip())

                cls._record_turn(
                    conversation,
                    canal,
                    message_text.strip(),
                    type_message,
                    greeting_result.message,
                )

                return greeting_result

            # ----------------------------------------------------
            # 2. Texte final
            # ----------------------------------------------------

            combined_text = cls._build_combined_text(
                message_text,
                image_bytes,
                audio_bytes,
            )

            logger.info(
                "[FemiRouterManager] "
                "Message final envoyé au Router: %s",
                combined_text,
            )

            # ----------------------------------------------------
            # 3. Routage
            # ----------------------------------------------------

            # Actions en attente de clarification (best-effort)
            pending = PendingTurn.load(conversation)

            router_output = RouterExecutor.execute(
                combined_text,
                history=history_text,
                **pending.router_kwargs(),
            )

            pending.start_turn(
                combined_text,
                len(router_output.intents),
            )

            logger.info(
                "[FemiRouterManager] "
                "Router terminé. Nombre d'intents=%s",
                len(router_output.intents),
            )

            # ----------------------------------------------------
            # 4. Dispatch + persistance
            # ----------------------------------------------------

            result = cls._build_result(
                router_output=router_output,
                entreprise=entreprise,
                utilisateur=utilisateur,
                source=source,
                raw_text=combined_text,
                image_bytes=image_bytes,
                pending=pending,
            )

            pending.commit()

            cls._record_turn(
                conversation,
                canal,
                combined_text,
                type_message,
                result.message,
            )

            return result

        except (
            Entreprise.DoesNotExist,
            Utilisateur.DoesNotExist,
        ) as exc:

            logger.error(
                "[FemiRouterManager] "
                "Contexte tenant introuvable (sync): %s",
                exc,
            )

            return RouterProcessResult(
                success=False,
                message=str(exc),
            )

        except RouterInputError as exc:

            logger.warning(
                "[FemiRouterManager] "
                "Aucune donnée exploitable (sync): %s",
                exc,
            )

            return RouterProcessResult(
                success=False,
                message=str(exc),
            )

        except (
            OcrExecutionError,
            SttExecutionError,
            ValueError,
        ) as exc:

            logger.error(
                "[FemiRouterManager] "
                "Échec OCR/audio (sync): %s",
                exc,
            )

            return RouterProcessResult(
                success=False,
                message=(
                    "Le traitement de l'image ou de l'audio "
                    "a échoué."
                ),
            )

        except RouterExecutionError as exc:

            logger.error(
                "[FemiRouterManager] "
                "Échec du Router (sync): %s",
                exc,
            )

            return RouterProcessResult(
                success=False,
                message="Le routage du message a échoué.",
            )

        except Exception:

            logger.exception(
                "[FemiRouterManager] "
                "Erreur inattendue (sync)"
            )

            return RouterProcessResult(
                success=False,
                message="Une erreur technique est survenue.",
            )

    # ============================================================
    # CONSTRUCTION DU TEXTE
    # ============================================================

    @classmethod
    def _build_combined_text(
        cls,
        text_input: Optional[str],
        image_bytes: Optional[bytes],
        audio_bytes: Optional[bytes],
    ) -> str:

        parts: list[str] = []

        # --------------------------------------------------------
        # Texte
        # --------------------------------------------------------

        if text_input and text_input.strip():
            parts.append(text_input.strip())

        # --------------------------------------------------------
        # OCR
        # --------------------------------------------------------

        if image_bytes:

            ocr_result = OcrExecutor.execute(
                image_bytes
            )

            if ocr_result.texte_brut_complet:

                parts.append(
                    ocr_result.texte_brut_complet
                )

            else:

                readable_text = (
                    cls._ocr_result_to_readable_text(
                        ocr_result
                    )
                )

                if readable_text:
                    parts.append(readable_text)

        # --------------------------------------------------------
        # AUDIO / STT
        # --------------------------------------------------------

        if audio_bytes:

            raw_transcription = transcribe_audio(
                audio_bytes
            )

            stt_result = SttExecutor.execute(
                raw_transcription
            )

            if stt_result.corrected_text:

                parts.append(
                    stt_result.corrected_text
                )

        # --------------------------------------------------------
        # Résultat final
        # --------------------------------------------------------

        combined = "\n".join(parts).strip()

        if not combined:

            raise RouterInputError(
                "Aucune donnée textuelle ou multimédia "
                "exploitable."
            )

        return combined

    # ============================================================
    # OCR → TEXTE
    # ============================================================

    @staticmethod
    def _ocr_result_to_readable_text(
        ocr_result,
    ) -> str:

        lines: list[str] = []

        en_tete = ocr_result.en_tete

        if en_tete.nom_commercant:
            lines.append(
                f"Commerçant : "
                f"{en_tete.nom_commercant}"
            )

        if en_tete.numero_facture_recu:
            lines.append(
                f"Référence : "
                f"{en_tete.numero_facture_recu}"
            )

        if en_tete.date:
            lines.append(
                f"Date : {en_tete.date}"
            )

        for ligne in ocr_result.lignes_articles:

            if not ligne.designation:
                continue

            detail = ligne.designation

            if ligne.quantite:

                detail += (
                    f" (quantité : "
                    f"{ligne.quantite}"
                )

                if ligne.prix_unitaire:

                    detail += (
                        f", prix unitaire : "
                        f"{ligne.prix_unitaire}"
                    )

                detail += ")"

            if ligne.prix_total:

                detail += (
                    f" — total : "
                    f"{ligne.prix_total}"
                )

            lines.append(detail)

        totaux = ocr_result.totaux

        if totaux.total_ht:

            lines.append(
                f"Total HT : "
                f"{totaux.total_ht}"
            )

        if totaux.tva:

            lines.append(
                f"TVA : {totaux.tva}"
            )

        if totaux.total_ttc:

            lines.append(
                f"Total TTC : "
                f"{totaux.total_ttc}"
            )

        if totaux.moyen_de_paiement:

            lines.append(
                f"Moyen de paiement : "
                f"{totaux.moyen_de_paiement}"
            )

        return "\n".join(lines)

    # ============================================================
    # VERSION ASYNCHRONE DU TEXTE
    # ============================================================

    @classmethod
    async def _abuild_combined_text(
        cls,
        text_input: Optional[str],
        image_bytes: Optional[bytes],
        audio_bytes: Optional[bytes],
    ) -> str:

        parts: list[str] = []

        # --------------------------------------------------------
        # Texte
        # --------------------------------------------------------

        if text_input and text_input.strip():
            parts.append(text_input.strip())

        # --------------------------------------------------------
        # OCR
        # --------------------------------------------------------

        if image_bytes:

            ocr_result = await OcrExecutor.aexecute(
                image_bytes
            )

            if ocr_result.texte_brut_complet:

                parts.append(
                    ocr_result.texte_brut_complet
                )

            else:

                readable_text = (
                    cls._ocr_result_to_readable_text(
                        ocr_result
                    )
                )

                if readable_text:
                    parts.append(readable_text)

        # --------------------------------------------------------
        # AUDIO / STT
        # --------------------------------------------------------

        if audio_bytes:

            raw_transcription = await sync_to_async(
                transcribe_audio
            )(audio_bytes)

            stt_result = await SttExecutor.aexecute(
                raw_transcription
            )

            if stt_result.corrected_text:

                parts.append(
                    stt_result.corrected_text
                )

        combined = "\n".join(parts).strip()

        if not combined:

            raise RouterInputError(
                "Aucune donnée textuelle ou multimédia "
                "exploitable."
            )

        return combined

    # ============================================================
    # TENANT / UTILISATEUR
    # ============================================================

    @classmethod
    def _get_tenant_context_stricte(
        cls,
        entreprise_id: Optional[str],
        utilisateur_id: Optional[str],
    ) -> Tuple[Entreprise, Utilisateur]:

        entreprise = cls._get_entreprise(entreprise_id)

        utilisateur = (
            cls._get_utilisateur_stricte(
                utilisateur_id
            )
        )

        return entreprise, utilisateur

    @staticmethod
    def _get_entreprise(entreprise_id: Optional[str]) -> Entreprise:
        """Entreprise du tenant, ou Entreprise.DoesNotExist si absente."""
        if entreprise_id:
            return Entreprise.objects.get(id=entreprise_id)
        raise Entreprise.DoesNotExist("Aucun ID d'entreprise fourni.")

    @classmethod
    def _get_utilisateur_stricte(
        cls,
        utilisateur_id: Optional[str],
    ) -> Utilisateur:

        if utilisateur_id:

            utilisateur = (
                Utilisateur.objects
                .filter(id=utilisateur_id)
                .first()
            )

            if utilisateur is not None:
                return utilisateur

        raise Utilisateur.DoesNotExist(
            "Aucun utilisateur résolu pour "
            f"utilisateur_id={utilisateur_id!r}."
        )

    # ============================================================
    # CONTEXTE CONVERSATIONNEL (historique multi-canal)
    # ============================================================
    #
    # Best-effort par construction : une panne ici ne doit jamais faire
    # échouer le traitement principal d'un message (voir
    # conversation_manager.py). Utilisé par route_message() ET
    # aroute_message() (ce dernier passe par sync_to_async).

    @classmethod
    def _setup_conversation_context(
        cls,
        utilisateur: Utilisateur,
        entreprise: Entreprise,
        source: str,
        image_bytes: Optional[bytes],
        audio_bytes: Optional[bytes],
    ) -> Tuple[
        Optional[Conversation],
        str,
        str,
        Optional[str],
    ]:
        """Résout/crée la Conversation active et l'historique à donner
        au Router.

        Retourne (conversation, canal, type_message, history_text).
        `conversation` vaut None en cas d'échec (aucun blocage du flux
        principal), auquel cas les enregistrements de messages sont
        simplement ignorés en aval.
        """

        canal = normalize_canal(source)
        type_message = resolve_type_message(
            image_bytes,
            audio_bytes,
        )

        try:
            conversation = get_or_create_active_conversation(
                utilisateur,
                entreprise,
                canal,
            )

            history_text = build_history_text(conversation)

            return conversation, canal, type_message, history_text

        except Exception:

            logger.exception(
                "[FemiRouterManager] "
                "Échec de préparation du contexte conversationnel "
                "— poursuite sans historique."
            )

            return None, canal, type_message, None

    @classmethod
    def _record_turn(
        cls,
        conversation: Optional[Conversation],
        canal: str,
        user_text: str,
        user_type_message: str,
        agent_text: Optional[str],
    ) -> None:
        """Enregistre le tour utilisateur + agent dans l'historique
        (version synchrone). Aucune exception ne remonte au-delà de
        record_message(), qui est déjà défensif."""

        if conversation is None:
            return

        record_message(
            conversation,
            canal,
            "USER",
            user_text,
            user_type_message,
        )

        record_message(
            conversation,
            canal,
            "AGENT",
            agent_text,
            "TEXTE",
        )

    @classmethod
    async def _arecord_turn(
        cls,
        conversation: Optional[Conversation],
        canal: str,
        user_text: str,
        user_type_message: str,
        agent_text: Optional[str],
    ) -> None:
        """Équivalent asynchrone de _record_turn()."""

        if conversation is None:
            return

        await sync_to_async(cls._record_turn)(
            conversation,
            canal,
            user_text,
            user_type_message,
            agent_text,
        )

    # ============================================================
    # DISPATCH SYNCHRONE
    # ============================================================

    @classmethod
    def _dispatch_intent(
        cls,
        intent: RouterIntent,
        entreprise: Entreprise,
        from_document: bool = False,
        segment: Optional[str] = None,
    ) -> Tuple[DispatchResult, bool]:

        # Texte envoyé à l'agent : segment fusionné avec l'action en
        # attente (voir pending_action.py) ou, à défaut, celui du Router.
        text = segment or intent.raw_segment

        logger.info(
            "[FemiRouterManager] "
            "Dispatch intent: agent=%s | segment=%s",
            intent.agent,
            intent.raw_segment,
        )

        if intent.agent == "ACCOUNTING":

            result = AccountingExecutor.execute(
                text,
                entreprise,
                from_document=from_document,
            )

            logger.info(
                "[FemiRouterManager] "
                "AccountingExecutor résultat=%s",
                result,
            )

            logger.info(
                "[FemiRouterManager] "
                "Accounting needs_clarification=%s",
                getattr(
                    result,
                    "needs_clarification",
                    None,
                ),
            )

            return (
                result,
                result.needs_clarification,
            )

        if intent.agent == "FINANCIAL_ANALYST":

            result = (
                FinancialAnalystExecutor.execute(
                    text,
                    entreprise,
                )
            )

            return (
                result,
                getattr(
                    result,
                    "needs_clarification",
                    False,
                ),
            )

        if intent.agent == "ACCOUNTING_MODIFY":

            result = (
                AccountingModifyExecutor.execute(
                    text,
                    entreprise,
                )
            )

            return (
                result,
                result.needs_clarification,
            )

        if intent.agent == "CUSTOMER":

            result = CustomerExecutor.execute(
                text,
                entreprise,
            )

            return (
                result,
                getattr(
                    result,
                    "needs_clarification",
                    False,
                ),
            )

        logger.warning(
            "[FemiRouterManager] "
            "Agent '%s' pas encore implémenté "
            "(intent_id=%s).",
            intent.agent,
            intent.intent_id,
        )

        return None, True

    # ============================================================
    # DISPATCH ASYNCHRONE
    # ============================================================

    @classmethod
    async def _adispatch_intent(
        cls,
        intent: RouterIntent,
        entreprise: Entreprise,
        from_document: bool = False,
        segment: Optional[str] = None,
    ) -> Tuple[DispatchResult, bool]:

        # Texte envoyé à l'agent : segment fusionné avec l'action en
        # attente (voir pending_action.py) ou, à défaut, celui du Router.
        text = segment or intent.raw_segment

        if intent.agent == "ACCOUNTING":

            result = await AccountingExecutor.aexecute(
                text,
                entreprise,
                from_document=from_document,
            )

            return (
                result,
                result.needs_clarification,
            )

        if intent.agent == "FINANCIAL_ANALYST":

            result = (
                await FinancialAnalystExecutor.aexecute(
                    text,
                    entreprise,
                )
            )

            return (
                result,
                getattr(
                    result,
                    "needs_clarification",
                    False,
                ),
            )

        if intent.agent == "ACCOUNTING_MODIFY":

            result = (
                await AccountingModifyExecutor.aexecute(
                    text,
                    entreprise,
                )
            )

            return (
                result,
                result.needs_clarification,
            )

        if intent.agent == "CUSTOMER":

            result = await CustomerExecutor.aexecute(
                text,
                entreprise,
            )

            return (
                result,
                getattr(
                    result,
                    "needs_clarification",
                    False,
                ),
            )

        logger.warning(
            "[FemiRouterManager] "
            "Agent '%s' pas encore implémenté "
            "(intent_id=%s).",
            intent.agent,
            intent.intent_id,
        )

        return None, True

    # ============================================================
    # SALUTATIONS (court-circuit, avant tout appel LLM)
    # ============================================================

    @classmethod
    def _is_pure_greeting(cls, text: str) -> bool:
        """Message purement social (salutation, merci, au revoir, « qui
        es-tu ? »). Vocabulaire fermé et aucun chiffre : voir
        social_replies.py. Le nom est conservé pour les appelants."""
        return classify_social_message(text) is not None

    @staticmethod
    def _greeting_result(text: str = "") -> RouterProcessResult:
        hour = timezone.localtime().hour
        return RouterProcessResult(
            success=True,
            message=build_social_reply(text, hour),
        )

    # ============================================================
    # CONSTRUCTION DU MESSAGE FINAL EN LANGAGE NATUREL
    # ============================================================
    #
    # Sans ceci, RouterProcessResult.message était une chaîne fixe
    # ("Message routé avec succès.") quel que soit le résultat réel —
    # l'utilisateur recevait la même phrase pour une transaction
    # enregistrée, une vraie réponse d'analyse financière (pourtant déjà
    # rédigée par le LLM dans FinalAnswerOutput.answer), ou une question
    # hors-sujet. Les méthodes ci-dessous assemblent un message réellement
    # représentatif, à partir des résultats déjà collectés par
    # _build_result()/_abuild_result().

    _TRANSACTION_TYPE_LABELS = {
        "RECETTE": "Recette",
        "DEPENSE": "Dépense",
        "PRET_DONNE": "Prêt donné",
        "PRET_RECU": "Prêt reçu",
    }

    @staticmethod
    def _format_montant(amount, currency: Optional[str]) -> str:
        devise = currency or "FCFA"
        if amount is None:
            return f"montant non précisé ({devise})"
        formatted = f"{amount:,.0f}".replace(",", " ")
        return f"{formatted} {devise}"

    @classmethod
    def _summarize_accounting_transactions(cls, result: AccountingExtractionResult) -> list[str]:
        lines = []
        for txn in result.transactions:
            label = cls._TRANSACTION_TYPE_LABELS.get(txn.transaction_type, txn.transaction_type)
            montant = cls._format_montant(txn.amount_ttc, txn.currency)
            extra = f" ({txn.category})" if txn.category else ""
            contact = f" — {txn.contact}" if txn.contact else ""
            statut = getattr(txn, "statut_paiement", "PAYE")
            avance = getattr(txn, "montant_deja_paye", None)
            if (
                statut == "CREDIT"
                and avance
                and txn.amount_ttc
                and 0 < avance < txn.amount_ttc
                and txn.transaction_type in ("RECETTE", "DEPENSE")
            ):
                reste = cls._format_montant(txn.amount_ttc - avance, txn.currency)
                deja = cls._format_montant(avance, txn.currency)
                if txn.transaction_type == "RECETTE":
                    lines.append(
                        f"✅ Vente de {montant}{extra}{contact} enregistrée.\n"
                        f"Avance reçue : {deja}. Il reste {reste} à encaisser."
                    )
                else:
                    lines.append(
                        f"✅ Achat de {montant}{extra}{contact} enregistré.\n"
                        f"Avance versée : {deja}. Il reste {reste} à payer."
                    )
            elif statut == "CREDIT" and txn.transaction_type == "RECETTE":
                lines.append(
                    f"✅ Vente à crédit de {montant}{extra}{contact} enregistrée.\n"
                    f"Créance client : {montant} restent à encaisser."
                )
            elif statut == "CREDIT" and txn.transaction_type == "DEPENSE":
                lines.append(
                    f"✅ Achat à crédit de {montant}{extra}{contact} enregistré.\n"
                    f"Dette fournisseur : {montant} restent à payer."
                )
            else:
                lines.append(f"✅ {label} de {montant}{extra}{contact} enregistrée.")
        return lines

    @classmethod
    def _summarize_customer_payment(cls, result: CustomerExtractionOutput) -> str:
        montant = cls._format_montant(result.amount_ttc, result.currency)
        contact = result.contact or "ce contact"
        return f"✅ Paiement de {montant} enregistré pour {contact}."

    @staticmethod
    def _summarize_propose_change(result: AccountingModifyProposeChangeResult) -> str:
        if result.action_type == "DELETE":
            return (
                f"🗑️ Je propose de supprimer cette opération : {result.current_values}. "
                "Confirmes-tu ?"
            )
        return (
            f"✏️ Je propose de modifier cette opération : {result.proposed_values}. "
            "Confirmes-tu ?"
        )

    @staticmethod
    def _summarize_candidates(result: AccountingModifyResolutionResult) -> str:
        if not result.candidates:
            return "Je n'ai trouvé aucune opération correspondante."
        candidats = "\n".join(f"- {c.summary}" for c in result.candidates)
        return f"Plusieurs opérations correspondent, laquelle veux-tu modifier/supprimer ?\n{candidats}"

    # ------------------------------------------------------------
    # QUESTIONS DE CLARIFICATION (codes missing_fields → français clair)
    # ------------------------------------------------------------
    # Les codes viennent des prompts (ROUTER, ACCOUNTING, ACCOUNTING_MODIFY,
    # CUSTOMER, FINANCIAL_ANALYST). L'utilisateur ne doit JAMAIS voir un nom
    # de champ technique. Ordre = priorité : ce qui bloque l'enregistrement
    # d'abord. Texte fixe (pas de LLM) : réponse identique et fiable pour
    # une application comptable.
    _MISSING_FIELD_QUESTIONS = {
        "transaction_type": "Dis-moi, c'est une vente que tu as faite, ou un achat pour ton activité ? (Si c'est un prêt, précise-le-moi.)",
        "amount_ttc": "Quel est le montant total, s'il te plaît ?",
        "nature_creance": "C'est le remboursement d'une vente à crédit, ou d'un prêt que tu avais accordé ?",
        "statut_paiement": "Est-ce que c'est déjà réglé en totalité, ou est-ce qu'il reste une partie à payer ? S'il y a eu une avance, dis-moi combien.",
        "contact_disambiguation": "J'ai plusieurs personnes avec ce nom. C'est laquelle ? Donne-moi son nom complet.",
        "target_data": "Quelle opération veux-tu modifier ou supprimer ? Donne-moi le montant, la date ou le nom du contact.",
        "search_criteria": "Quelle opération cherches-tu ? Donne-moi le montant, la date ou le nom du contact.",
        "operation_not_found": "Je ne retrouve pas cette opération. Peux-tu donner le montant, la date ou le nom du contact ?",
        "new_value": "Quelle est la nouvelle valeur à enregistrer ?",
        "category_not_available": "Cette catégorie n'existe pas dans ton compte. Peux-tu en choisir une existante ?",
        "payment_method_invalid": "Quel est le mode de paiement : espèces, mobile money, virement, chèque ou carte ?",
        "transaction_type_reclassification_not_supported": (
            "Je ne peux pas changer le type d'une opération déjà enregistrée. "
            "Annule-la puis enregistre-la de nouveau avec le bon type."
        ),
        "indicator": "Quel indicateur veux-tu : chiffre d'affaires, dépenses, bénéfice, marge ou trésorerie ?",
        "indicateur": "Quel indicateur veux-tu : chiffre d'affaires, dépenses, bénéfice, marge ou trésorerie ?",
        "date_range_incomplete": "Pour quelle période exactement ? (ex : ce mois-ci, septembre, du 1er au 15)",
        "annee": "Pour quelle année ?",
        "wrong_agent": "Peux-tu reformuler ta demande en précisant ce que tu veux faire ?",
        "search_error": "Je n'ai pas réussi à retrouver cette information. Peux-tu reformuler ?",
    }

    # Codes déjà rendus ailleurs (liste de candidats) ou propres à un autre
    # message : jamais transformés en question ici.
    _CLARIFICATION_SKIP_CODES = {"fonctionnalite_non_disponible", "operation_disambiguation"}

    @classmethod
    def _build_understood_recap(cls, accounting_results) -> str | None:
        """Phrase courte de ce que Femi a déjà compris (montant, contact),
        pour ne pas redemander ce qu'il sait et rassurer l'utilisateur."""

        for result in accounting_results or []:
            if not getattr(result, "needs_clarification", False):
                continue
            for txn in getattr(result, "transactions", []) or []:
                if not txn.amount_ttc:
                    continue
                montant = cls._format_montant(txn.amount_ttc, txn.currency)
                contact = f" ({txn.contact})" if txn.contact else ""
                avance = getattr(txn, "montant_deja_paye", None)
                if avance:
                    return (
                        f"J'ai bien lu ta facture de {montant}{contact} "
                        f"et je vois une avance de "
                        f"{cls._format_montant(avance, txn.currency)}."
                    )
                return f"J'ai bien lu ton document : {montant}{contact}."

        return None

    @classmethod
    def _build_clarification_message(
        cls,
        missing_fields: list[str],
        accounting_results=None,
    ) -> str | None:
        """Question(s) en français à partir des codes missing_fields.

        Retourne None s'il n'y a rien à demander. Un code inconnu ne produit
        jamais son nom technique : il est remplacé par une demande générique.
        """
        questions: list[str] = []
        has_unknown = False
        for code in missing_fields:
            if code in cls._CLARIFICATION_SKIP_CODES:
                continue
            question = cls._MISSING_FIELD_QUESTIONS.get(code)
            if question is None:
                has_unknown = True
            elif question not in questions:
                questions.append(question)

        if not questions and not has_unknown:
            return None
        if not questions:
            return "Il me manque une petite précision pour continuer. Peux-tu m'en dire un peu plus ?"

        questions = questions[:3]
        recap = cls._build_understood_recap(accounting_results)
        if len(questions) == 1:
            intro = f"{recap} Une petite précision et j'enregistre :" if recap else "Une petite précision pour bien l'enregistrer :"
            return f"{intro}\n{questions[0]}"
        liste = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, start=1))
        intro = f"{recap} Deux ou trois précisions et j'enregistre :" if recap else "Pour bien l'enregistrer, j'ai juste besoin de quelques précisions :"
        return f"{intro}\n{liste}"

    @classmethod
    def _build_final_message(
        cls,
        accounting_results: list[AccountingExtractionResult],
        financial_analyst_results: list,
        accounting_modify_results: list,
        customer_results: list,
        needs_clarification: bool,
        missing_fields: list[str],
    ) -> str:
        """Assemble le message réellement envoyé à l'utilisateur (WhatsApp et
        application mobile) à partir des résultats structurés des agents."""
        lines: list[str] = []

        # 1. Réponses conversationnelles déjà rédigées par un agent
        #    (FinancialAnalystExecutor ou sous-flux READ de CustomerExecutor).
        for result in list(financial_analyst_results) + list(customer_results):
            if isinstance(result, FinalAnswerOutput):
                lines.append(result.answer)

        # 2. Transactions comptables effectivement enregistrées (ACCOUNTING).
        for result in accounting_results:
            if not result.needs_clarification:
                lines.extend(cls._summarize_accounting_transactions(result))

        # 3. Paiement client/fournisseur enregistré (CUSTOMER, sous-flux CREATE).
        for result in customer_results:
            if isinstance(result, CustomerExtractionOutput) and not result.needs_clarification:
                lines.append(cls._summarize_customer_payment(result))

        # 4. Propositions de modification/suppression (ACCOUNTING_MODIFY),
        #    ou liste de candidats en cas d'ambiguïté.
        for result in accounting_modify_results:
            if isinstance(result, AccountingModifyProposeChangeResult):
                lines.append(cls._summarize_propose_change(result))
            elif isinstance(result, AccountingModifyResolutionResult) and result.candidates:
                lines.append(cls._summarize_candidates(result))

        if lines:
            # Une opération du lot peut être enregistrée pendant qu'une autre
            # attend une précision : ne pas perdre la question.
            if needs_clarification:
                clarification = cls._build_clarification_message(
                    missing_fields, accounting_results
                )
                if clarification:
                    lines.append(clarification)
            return "\n\n".join(lines)

        # 5. Rien de concret à annoncer : soit une clarification est
        #    nécessaire, soit la demande n'a pas pu être rattachée à un agent
        #    connu (question hors-sujet, intent SETTINGS/UNKNOWN...).
        if needs_clarification:
            if "fonctionnalite_non_disponible" in missing_fields:
                return (
                    "Cette fonctionnalité n'est pas encore disponible chez Femi 🙂\n\n"
                    "Je peux en revanche t'aider avec :\n"
                    "• Ton chiffre d'affaires, tes dépenses (avec détail par catégorie), "
                    "ton bénéfice, ta marge ou ta trésorerie ;\n"
                    "• Une comparaison entre deux périodes ;\n"
                    "• Tes créances clients et qui relancer ;\n"
                    "• Enregistrer, modifier ou supprimer une transaction."
                )
            clarification = cls._build_clarification_message(
                    missing_fields, accounting_results
                )
            if clarification:
                return clarification
            return "Je n'ai pas toutes les informations nécessaires. Peux-tu préciser ta demande ?"

        return build_off_topic_reply()

    # ============================================================
    # CONSTRUCTION RESULTAT SYNCHRONE
    # ============================================================

    @classmethod
    def _build_result(
        cls,
        router_output: RouterOutput,
        entreprise: Entreprise,
        utilisateur: Optional[Utilisateur] = None,
        source: str = "API",
        raw_text: str = "",
        image_bytes: Optional[bytes] = None,
        pending: Optional[PendingTurn] = None,
    ) -> RouterProcessResult:

        logger.info("=" * 80)
        logger.info(
            "[FemiRouterManager] >>> DEBUT _build_result"
        )

        logger.info(
            "[FemiRouterManager] entreprise_id=%s",
            getattr(entreprise, "id", None),
        )

        logger.info(
            "[FemiRouterManager] utilisateur_id=%s",
            getattr(utilisateur, "id", None),
        )

        logger.info(
            "[FemiRouterManager] source=%s",
            source,
        )

        logger.info(
            "[FemiRouterManager] raw_text=%s",
            raw_text,
        )

        needs_clarification = False
        missing_fields: list[str] = []

        accounting_results: list[
            AccountingExtractionResult
        ] = []

        financial_analyst_results: list[
            ToolSelectionOutput | FinalAnswerOutput
        ] = []

        accounting_modify_results: list[
            AccountingModifySearchResult
            | AccountingModifyResolutionResult
            | AccountingModifyProposeChangeResult
        ] = []

        customer_results: list[
            ValidationOutput
            | CustomerExtractionOutput
            | ToolSelectionOutput
            | FinalAnswerOutput
        ] = []

        logger.info(
            "[FemiRouterManager] "
            "Nombre d'intents=%s",
            len(router_output.intents),
        )

        # --------------------------------------------------------
        # Dispatch
        # --------------------------------------------------------

        for index, intent in enumerate(
            router_output.intents,
            start=1,
        ):

            logger.info("-" * 70)

            logger.info(
                "[FemiRouterManager] "
                ">>> INTENT #%s",
                index,
            )

            logger.info(
                "[FemiRouterManager] agent=%s",
                intent.agent,
            )

            logger.info(
                "[FemiRouterManager] action=%s",
                getattr(
                    intent,
                    "action",
                    None,
                ),
            )

            logger.info(
                "[FemiRouterManager] raw_segment=%s",
                intent.raw_segment,
            )

            logger.info(
                "[FemiRouterManager] "
                "intent.needs_clarification=%s",
                intent.needs_clarification,
            )

            logger.info(
                "[FemiRouterManager] "
                "intent.missing_fields=%s",
                intent.missing_fields,
            )

            if intent.needs_clarification:
                needs_clarification = True

            for field in intent.missing_fields:

                if field not in missing_fields:
                    missing_fields.append(field)

            # ----------------------------------------------------
            # Dispatch agent
            # ----------------------------------------------------

            try:

                segment, doc_flag = (
                    pending.resolve(intent, bool(image_bytes))
                    if pending is not None
                    else (intent.raw_segment, bool(image_bytes))
                )

                result, dispatch_needs_clarification = (
                    cls._dispatch_intent(
                        intent,
                        entreprise,
                        from_document=doc_flag,
                        segment=segment,
                    )
                )

                if pending is not None:
                    pending.observe(
                        intent,
                        segment,
                        doc_flag,
                        bool(dispatch_needs_clarification)
                        and result is not None,
                        getattr(result, "missing_fields", None),
                    )

            except Exception:

                logger.exception(
                    "[FemiRouterManager] "
                    "ERREUR pendant _dispatch_intent "
                    "pour intent #%s",
                    index,
                )

                raise

            logger.info(
                "[FemiRouterManager] "
                "Résultat dispatch=%s",
                result,
            )

            logger.info(
                "[FemiRouterManager] "
                "Type résultat=%s",
                (
                    type(result).__name__
                    if result is not None
                    else "None"
                ),
            )

            logger.info(
                "[FemiRouterManager] "
                "dispatch_needs_clarification=%s",
                dispatch_needs_clarification,
            )

            if dispatch_needs_clarification:
                needs_clarification = True

            # ----------------------------------------------------
            # Classification du résultat
            # ----------------------------------------------------

            if isinstance(
                result,
                AccountingExtractionResult,
            ):

                logger.info(
                    "[FemiRouterManager] "
                    ">>> AccountingExtractionResult détecté"
                )

                logger.info(
                    "[FemiRouterManager] "
                    "Accounting result=%s",
                    result,
                )

                logger.info(
                    "[FemiRouterManager] "
                    "Accounting needs_clarification=%s",
                    getattr(
                        result,
                        "needs_clarification",
                        None,
                    ),
                )

                accounting_results.append(
                    result
                )

            elif isinstance(
                result,
                (
                    AccountingModifySearchResult,
                    AccountingModifyResolutionResult,
                    AccountingModifyProposeChangeResult,
                ),
            ):

                logger.info(
                    "[FemiRouterManager] "
                    ">>> AccountingModifyResult détecté"
                )

                accounting_modify_results.append(
                    result
                )

            elif intent.agent == "CUSTOMER":

                logger.info(
                    "[FemiRouterManager] "
                    ">>> Customer result détecté"
                )

                if result is not None:
                    customer_results.append(result)

            elif isinstance(
                result,
                (
                    ToolSelectionOutput,
                    FinalAnswerOutput,
                ),
            ):

                logger.info(
                    "[FemiRouterManager] "
                    ">>> Financial Analyst result détecté"
                )

                financial_analyst_results.append(
                    result
                )

            # ----------------------------------------------------
            # Champs manquants de l'agent
            # ----------------------------------------------------

            if (
                result is not None
                and hasattr(
                    result,
                    "missing_fields",
                )
            ):

                result_missing_fields = (
                    getattr(
                        result,
                        "missing_fields",
                        [],
                    )
                )

                logger.info(
                    "[FemiRouterManager] "
                    "result.missing_fields=%s",
                    result_missing_fields,
                )

                for field in result_missing_fields:

                    if field not in missing_fields:
                        missing_fields.append(field)

        # ========================================================
        # FIN DISPATCH
        # ========================================================

        logger.info("=" * 80)

        logger.info(
            "[FemiRouterManager] "
            ">>> accounting_results FINAL=%s",
            accounting_results,
        )

        logger.info(
            "[FemiRouterManager] "
            ">>> nombre accounting_results=%s",
            len(accounting_results),
        )

        # --------------------------------------------------------
        # Filtrage des résultats valides
        # --------------------------------------------------------

        clean_accounting_results = [
            result
            for result in accounting_results
            if not result.needs_clarification
        ]

        logger.info(
            "[FemiRouterManager] "
            ">>> clean_accounting_results=%s",
            clean_accounting_results,
        )

        logger.info(
            "[FemiRouterManager] "
            ">>> nombre clean_accounting_results=%s",
            len(clean_accounting_results),
        )

        # --------------------------------------------------------
        # Log détaillé de chaque résultat
        # --------------------------------------------------------

        if accounting_results:

            for index, result in enumerate(
                accounting_results,
                start=1,
            ):

                logger.info(
                    "[FemiRouterManager] "
                    "Accounting #%s | "
                    "needs_clarification=%s | "
                    "result=%s",
                    index,
                    getattr(
                        result,
                        "needs_clarification",
                        None,
                    ),
                    result,
                )

        else:

            logger.warning(
                "[FemiRouterManager] "
                "!!! Aucun AccountingExtractionResult "
                "n'a été produit."
            )

        # ========================================================
        # PERSISTANCE ACCOUNTING
        # ========================================================

        operation_ids: list[str] = []

        if clean_accounting_results:

            logger.info("=" * 80)

            logger.info(
                "[FemiRouterManager] "
                ">>> SAUVEGARDE DES OPERATIONS"
            )

            logger.info(
                "[FemiRouterManager] "
                "Nombre de résultats à sauvegarder=%s",
                len(clean_accounting_results),
            )

            logger.info(
                "[FemiRouterManager] "
                "Utilisateur présent=%s",
                utilisateur is not None,
            )

            logger.info(
                "[FemiRouterManager] "
                "Utilisateur ID=%s",
                getattr(
                    utilisateur,
                    "id",
                    None,
                ),
            )

            logger.info(
                "[FemiRouterManager] "
                "Entreprise ID=%s",
                getattr(
                    entreprise,
                    "id",
                    None,
                ),
            )

            if utilisateur is None:

                logger.error(
                    "[FemiRouterManager] "
                    "!!! IMPOSSIBLE DE SAUVEGARDER : "
                    "utilisateur=None"
                )

            else:

                try:

                    logger.info(
                        "[FemiRouterManager] "
                        ">>> appel de "
                        "_persist_accounting_results()"
                    )

                    operations = (
                        cls._persist_accounting_results(
                            accounting_results=(
                                clean_accounting_results
                            ),
                            entreprise=entreprise,
                            utilisateur=utilisateur,
                            source=source,
                            raw_text=(
                                pending.effective_raw_text(raw_text)
                                if pending is not None
                                else raw_text
                            ),
                            image_bytes=image_bytes,
                        )
                    )

                    logger.info(
                        "[FemiRouterManager] "
                        ">>> _persist_accounting_results terminé"
                    )

                    logger.info(
                        "[FemiRouterManager] "
                        "operations retournées=%s",
                        operations,
                    )

                    logger.info(
                        "[FemiRouterManager] "
                        "nombre operations créées=%s",
                        len(operations),
                    )

                    operation_ids = [
                        str(operation.id)
                        for operation in operations
                    ]

                    logger.info(
                        "[FemiRouterManager] "
                        ">>> OPERATION IDS=%s",
                        operation_ids,
                    )

                except Exception:

                    logger.exception(
                        "[FemiRouterManager] "
                        "!!! ERREUR LORS DE LA "
                        "SAUVEGARDE DES OPERATIONS"
                    )

                    raise

        else:

            logger.warning("=" * 80)

            logger.warning(
                "[FemiRouterManager] "
                "!!! AUCUNE OPERATION À SAUVEGARDER"
            )

            logger.warning(
                "[FemiRouterManager] "
                "accounting_results=%s",
                accounting_results,
            )

            logger.warning(
                "[FemiRouterManager] "
                "missing_fields=%s",
                missing_fields,
            )

            logger.warning(
                "[FemiRouterManager] "
                "needs_clarification=%s",
                needs_clarification,
            )

        # ========================================================
        # RESULTAT FINAL
        # ========================================================

        logger.info("=" * 80)

        logger.info(
            "[FemiRouterManager] "
            ">>> FIN _build_result"
        )

        logger.info(
            "[FemiRouterManager] "
            "operation_ids=%s",
            operation_ids,
        )

        logger.info(
            "[FemiRouterManager] "
            "needs_clarification=%s",
            needs_clarification,
        )

        logger.info(
            "[FemiRouterManager] "
            "missing_fields=%s",
            missing_fields,
        )

        logger.info("=" * 80)

        return RouterProcessResult(
            success=True,
            message=cls._build_final_message(
                accounting_results=accounting_results,
                financial_analyst_results=financial_analyst_results,
                accounting_modify_results=accounting_modify_results,
                customer_results=customer_results,
                needs_clarification=needs_clarification,
                missing_fields=missing_fields,
            ),
            router_output=router_output,
            operation_ids=operation_ids,
            needs_clarification=needs_clarification,
            missing_fields=missing_fields,
            accounting_results=accounting_results,
            financial_analyst_results=(
                financial_analyst_results
            ),
            accounting_modify_results=(
                accounting_modify_results
            ),
            customer_results=customer_results,
        )

    # ============================================================
    # PERSISTANCE ACCOUNTING
    # ============================================================

    @classmethod
    def _persist_accounting_results(
        cls,
        accounting_results,
        entreprise,
        utilisateur,
        source,
        raw_text,
        image_bytes,
    ) -> list[Operation]:

        logger.info("=" * 80)

        logger.info(
            "[FemiRouterManager] "
            ">>> _persist_accounting_results"
        )

        logger.info(
            "[FemiRouterManager] "
            "utilisateur_id=%s",
            getattr(
                utilisateur,
                "id",
                None,
            ),
        )

        logger.info(
            "[FemiRouterManager] "
            "entreprise_id=%s",
            getattr(
                entreprise,
                "id",
                None,
            ),
        )

        logger.info(
            "[FemiRouterManager] "
            "source=%s",
            source,
        )

        logger.info(
            "[FemiRouterManager] "
            "raw_text=%s",
            raw_text,
        )

        logger.info(
            "[FemiRouterManager] "
            "nombre accounting_results=%s",
            len(accounting_results),
        )

        logger.info(
            "[FemiRouterManager] "
            "accounting_results=%s",
            accounting_results,
        )

        # --------------------------------------------------------
        # Vérification utilisateur
        # --------------------------------------------------------

        if utilisateur is None:

            logger.error(
                "[FemiRouterManager] "
                "Impossible de persister une opération "
                "sans utilisateur."
            )

            return []

        all_operations: list[Operation] = []

        # --------------------------------------------------------
        # Sauvegarde de chaque résultat comptable
        # --------------------------------------------------------

        for index, result in enumerate(
            accounting_results,
            start=1,
        ):

            logger.info("-" * 70)

            logger.info(
                "[FemiRouterManager] "
                ">>> Sauvegarde accounting #%s",
                index,
            )

            logger.info(
                "[FemiRouterManager] "
                "result=%s",
                result,
            )

            try:

                logger.info(
                    "[FemiRouterManager] "
                    ">>> appel save_accounting_transactions()"
                )

                operations = (
                    save_accounting_transactions(
                        entreprise,
                        utilisateur,
                        result,
                        source,
                        raw_text,
                    )
                )

                logger.info(
                    "[FemiRouterManager] "
                    "<<< retour save_accounting_transactions()=%s",
                    operations,
                )

                logger.info(
                    "[FemiRouterManager] "
                    "type retour=%s",
                    (
                        type(operations).__name__
                        if operations is not None
                        else "None"
                    ),
                )

                if operations:

                    logger.info(
                        "[FemiRouterManager] "
                        "Nombre d'operations retournées=%s",
                        len(operations),
                    )

                    all_operations.extend(
                        operations
                    )

                else:

                    logger.warning(
                        "[FemiRouterManager] "
                        "!!! save_accounting_transactions "
                        "n'a retourné aucune opération."
                    )

            except Exception:

                logger.exception(
                    "[FemiRouterManager] "
                    "!!! Erreur lors de la sauvegarde "
                    "des transactions ACCOUNTING."
                )

                raise

        # --------------------------------------------------------
        # Résultat de la sauvegarde
        # --------------------------------------------------------

        logger.info("=" * 80)

        logger.info(
            "[FemiRouterManager] "
            ">>> OPERATIONS CRÉÉES=%s",
            all_operations,
        )

        logger.info(
            "[FemiRouterManager] "
            ">>> NOMBRE OPERATIONS=%s",
            len(all_operations),
        )

        logger.info(
            "[FemiRouterManager] "
            ">>> IDS OPERATIONS=%s",
            [
                str(operation.id)
                for operation in all_operations
            ],
        )

        # --------------------------------------------------------
        # Pièce justificative
        # --------------------------------------------------------

        if image_bytes:

            logger.info(
                "[FemiRouterManager] "
                "Image présente : ajout des pièces justificatives."
            )

            for operation in all_operations:

                cls._attach_piece_justificative_bytes(
                    operation,
                    image_bytes,
                )

        else:

            logger.info(
                "[FemiRouterManager] "
                "Aucune image à attacher."
            )

        logger.info("=" * 80)

        return all_operations

    # ============================================================
    # RESULTAT ASYNCHRONE
    # ============================================================

    @classmethod
    async def _abuild_result(
        cls,
        router_output: RouterOutput,
        entreprise: Entreprise,
        utilisateur: Optional[Utilisateur] = None,
        source: str = "API",
        raw_text: str = "",
        image_bytes: Optional[bytes] = None,
        pending: Optional[PendingTurn] = None,
    ) -> RouterProcessResult:

        needs_clarification = False
        missing_fields: list[str] = []

        accounting_results: list[
            AccountingExtractionResult
        ] = []

        financial_analyst_results: list[
            ToolSelectionOutput | FinalAnswerOutput
        ] = []

        accounting_modify_results: list[
            AccountingModifySearchResult
            | AccountingModifyResolutionResult
            | AccountingModifyProposeChangeResult
        ] = []

        customer_results: list[
            ValidationOutput
            | CustomerExtractionOutput
            | ToolSelectionOutput
            | FinalAnswerOutput
        ] = []

        # --------------------------------------------------------
        # Dispatch
        # --------------------------------------------------------

        for intent in router_output.intents:

            if intent.needs_clarification:
                needs_clarification = True

            for field in intent.missing_fields:

                if field not in missing_fields:
                    missing_fields.append(field)

            segment, doc_flag = (
                pending.resolve(intent, bool(image_bytes))
                if pending is not None
                else (intent.raw_segment, bool(image_bytes))
            )

            result, dispatch_needs_clarification = (
                await cls._adispatch_intent(
                    intent,
                    entreprise,
                    from_document=doc_flag,
                    segment=segment,
                )
            )

            if pending is not None:
                pending.observe(
                    intent,
                    segment,
                    doc_flag,
                    bool(dispatch_needs_clarification)
                    and result is not None,
                    getattr(result, "missing_fields", None),
                )

            if dispatch_needs_clarification:
                needs_clarification = True

            # ----------------------------------------------------
            # Classification
            # ----------------------------------------------------

            if isinstance(
                result,
                AccountingExtractionResult,
            ):

                accounting_results.append(
                    result
                )

            elif isinstance(
                result,
                (
                    AccountingModifySearchResult,
                    AccountingModifyResolutionResult,
                    AccountingModifyProposeChangeResult,
                ),
            ):

                accounting_modify_results.append(
                    result
                )

            elif intent.agent == "CUSTOMER":

                if result is not None:
                    customer_results.append(result)

            elif isinstance(
                result,
                (
                    ToolSelectionOutput,
                    FinalAnswerOutput,
                ),
            ):

                financial_analyst_results.append(
                    result
                )

            # ----------------------------------------------------
            # Champs manquants
            # ----------------------------------------------------

            if (
                result is not None
                and hasattr(
                    result,
                    "missing_fields",
                )
            ):

                for field in result.missing_fields:

                    if field not in missing_fields:
                        missing_fields.append(field)

        # --------------------------------------------------------
        # PERSISTANCE ASYNCHRONE
        # --------------------------------------------------------

        valid_accounting_results = [
            result
            for result in accounting_results
            if not result.needs_clarification
        ]

        operation_ids: list[str] = []

        if (
            valid_accounting_results
            and utilisateur is not None
        ):

            operations = await sync_to_async(
                cls._persist_accounting_results
            )(
                accounting_results=(
                    valid_accounting_results
                ),
                entreprise=entreprise,
                utilisateur=utilisateur,
                source=source,
                raw_text=(
                    pending.effective_raw_text(raw_text)
                    if pending is not None
                    else raw_text
                ),
                image_bytes=image_bytes,
            )

            operation_ids = [
                str(operation.id)
                for operation in operations
            ]

        # --------------------------------------------------------
        # RESULTAT FINAL
        # --------------------------------------------------------

        return RouterProcessResult(
            success=True,
            message=cls._build_final_message(
                accounting_results=accounting_results,
                financial_analyst_results=financial_analyst_results,
                accounting_modify_results=accounting_modify_results,
                customer_results=customer_results,
                needs_clarification=needs_clarification,
                missing_fields=missing_fields,
            ),
            router_output=router_output,
            operation_ids=operation_ids,
            needs_clarification=needs_clarification,
            missing_fields=missing_fields,
            accounting_results=accounting_results,
            financial_analyst_results=(
                financial_analyst_results
            ),
            accounting_modify_results=(
                accounting_modify_results
            ),
            customer_results=customer_results,
        )

    # ============================================================
    # PIECE JUSTIFICATIVE
    # ============================================================

    @staticmethod
    def _attach_piece_justificative_bytes(
        operation: Operation,
        image_bytes: bytes,
    ) -> None:

        import time

        filename = (
            f"whatsapp_{operation.id}.jpg"
        )

        max_retries = 3
        public_url = None

        for attempt in range(
            1,
            max_retries + 1,
        ):

            try:

                public_url = upload_file(
                    content=image_bytes,
                    filename=filename,
                    content_type="image/jpeg",
                )

                break

            except Exception as exc:

                if attempt == max_retries:

                    logger.exception(
                        "[FemiRouterManager] "
                        "Échec définitif de l'upload "
                        "Supabase Storage après %d "
                        "tentatives pour l'opération %s",
                        max_retries,
                        operation.id,
                    )

                    return

                logger.warning(
                    "[FemiRouterManager] "
                    "Tentative %d/%d échouée pour "
                    "l'upload Supabase "
                    "(opération %s): %s. "
                    "Nouvelle tentative...",
                    attempt,
                    max_retries,
                    operation.id,
                    exc,
                )

                time.sleep(1)

        # --------------------------------------------------------
        # Création PieceJustificative
        # --------------------------------------------------------

        if public_url:

            try:

                PieceJustificative.objects.create(
                    operation=operation,
                    nom_fichier=filename,
                    url_fichier=public_url,
                    type_mime="image/jpeg",
                    taille_octets=len(image_bytes),
                )

            except Exception:

                logger.exception(
                    "[FemiRouterManager] "
                    "Échec de la création de "
                    "PieceJustificative pour "
                    "l'opération %s",
                    operation.id,
                )