"""OAuth Source Serializer"""

from django.urls.base import reverse_lazy
from django_filters.filters import BooleanFilter
from django_filters.filterset import FilterSet
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_field
from requests import RequestException
from rest_framework.decorators import action
from rest_framework.fields import BooleanField, CharField, ChoiceField, SerializerMethodField
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import ValidationError
from rest_framework.viewsets import ModelViewSet

from authentik.core.api.sources import SourceSerializer
from authentik.core.api.used_by import UsedByMixin
from authentik.core.api.utils import PassiveSerializer
from authentik.lib.utils.http import get_http_session
from authentik.sources.oauth.models import OAuthSource
from authentik.sources.oauth.types.registry import SourceType, registry


class SourceTypeSerializer(PassiveSerializer):
    """Serializer for SourceType"""

    name = CharField(required=True)
    verbose_name = CharField(required=True)
    urls_customizable = BooleanField()
    request_token_url = CharField(read_only=True, allow_null=True)
    authorization_url = CharField(read_only=True, allow_null=True)
    access_token_url = CharField(read_only=True, allow_null=True)
    profile_url = CharField(read_only=True, allow_null=True)
    oidc_well_known_url = CharField(read_only=True, allow_null=True)
    oidc_jwks_url = CharField(read_only=True, allow_null=True)


class OAuthSourceSerializer(SourceSerializer):
    """OAuth Source Serializer"""

    provider_type = ChoiceField(choices=registry.get_name_tuple())
    callback_url = SerializerMethodField()
    type = SerializerMethodField()

    def get_callback_url(self, instance: OAuthSource) -> str:
        """Get OAuth Callback URL"""
        relative_url = reverse_lazy(
            "authentik_sources_oauth:oauth-client-callback",
            kwargs={"source_slug": instance.slug},
        )
        if "request" not in self.context:
            return relative_url
        return self.context["request"].build_absolute_uri(relative_url)

    @extend_schema_field(SourceTypeSerializer)
    def get_type(self, instance: OAuthSource) -> SourceTypeSerializer:
        """Get source's type configuration"""
        return SourceTypeSerializer(instance.source_type).data

    def validate(self, attrs: dict) -> dict:
        session = get_http_session()
        source_type = registry.find_type(attrs["provider_type"])

        well_known = attrs.get("oidc_well_known_url") or source_type.oidc_well_known_url
        inferred_oidc_jwks_url = None

        if well_known and well_known != "":
            try:
                well_known_config = session.get(well_known)
                well_known_config.raise_for_status()
            except RequestException as exc:
                text = exc.response.text if exc.response else str(exc)
                raise ValidationError({"oidc_well_known_url": text}) from None
            config = well_known_config.json()
            if "issuer" not in config:
                raise ValidationError({"oidc_well_known_url": "Invalid well-known configuration"})
            field_map = {
                # authentik field to oidc field
                "authorization_url": "authorization_endpoint",
                "access_token_url": "token_endpoint",
                "profile_url": "userinfo_endpoint",
            }
            for ak_key, oidc_key in field_map.items():
                # Don't overwrite user-set values
                if ak_key in attrs and attrs[ak_key]:
                    continue
                attrs[ak_key] = config.get(oidc_key, "")
            inferred_oidc_jwks_url = config.get("jwks_uri", "")

        # Prefer user-entered URL to inferred URL to default URL
        jwks_url = attrs.get("oidc_jwks_url") or inferred_oidc_jwks_url or source_type.oidc_jwks_url
        if jwks_url and jwks_url != "":
            attrs["oidc_jwks_url"] = jwks_url
            try:
                jwks_config = session.get(jwks_url)
                jwks_config.raise_for_status()
            except RequestException as exc:
                text = exc.response.text if exc.response else str(exc)
                raise ValidationError({"oidc_jwks_url": text}) from None
            config = jwks_config.json()
            attrs["oidc_jwks"] = config

        provider_type = registry.find_type(attrs.get("provider_type", ""))
        for url in [
            "authorization_url",
            "access_token_url",
            "profile_url",
        ]:
            if getattr(provider_type, url, None) is None:
                if url not in attrs:
                    raise ValidationError(
                        f"{url} is required for provider {provider_type.verbose_name}"
                    )
        return attrs

    class Meta:
        model = OAuthSource
        fields = SourceSerializer.Meta.fields + [
            "group_matching_mode",
            "provider_type",
            "request_token_url",
            "authorization_url",
            "access_token_url",
            "profile_url",
            "consumer_key",
            "consumer_secret",
            "callback_url",
            "additional_scopes",
            "type",
            "oidc_well_known_url",
            "oidc_jwks_url",
            "oidc_jwks",
            "authorization_code_auth_method",
        ]
        extra_kwargs = {
            "consumer_secret": {"write_only": True},
            "request_token_url": {"allow_blank": True},
            "authorization_url": {"allow_blank": True},
            "access_token_url": {"allow_blank": True},
            "profile_url": {"allow_blank": True},
        }


class OAuthSourceFilter(FilterSet):
    """OAuth Source filter set"""

    has_jwks = BooleanFilter(label="Only return sources with JWKS data", method="filter_has_jwks")

    def filter_has_jwks(self, queryset, name, value):  # pragma: no cover
        """Only return sources with JWKS data"""
        return queryset.exclude(oidc_jwks__iexact="{}")

    class Meta:
        model = OAuthSource
        fields = [
            "pbm_uuid",
            "name",
            "slug",
            "enabled",
            "authentication_flow",
            "enrollment_flow",
            "policy_engine_mode",
            "user_matching_mode",
            "group_matching_mode",
            "provider_type",
            "request_token_url",
            "authorization_url",
            "access_token_url",
            "profile_url",
            "consumer_key",
            "additional_scopes",
        ]


class OAuthSourceViewSet(UsedByMixin, ModelViewSet):
    """Source Viewset"""

    queryset = OAuthSource.objects.all()
    serializer_class = OAuthSourceSerializer
    lookup_field = "slug"
    filterset_class = OAuthSourceFilter
    search_fields = ["name", "slug"]
    ordering = ["name"]

    @extend_schema(
        responses={200: SourceTypeSerializer(many=True)},
        parameters=[
            OpenApiParameter(
                name="name",
                location=OpenApiParameter.QUERY,
                type=OpenApiTypes.STR,
            )
        ],
    )
    @action(detail=False, pagination_class=None, filter_backends=[])
    def source_types(self, request: Request) -> Response:
        """Get all creatable source types. If ?name is set, only returns the type for <name>.
        If <name> isn't found, returns the default type."""
        data = []
        if "name" in request.query_params:
            source_type = registry.find_type(request.query_params.get("name"))
            if source_type.__class__ != SourceType:
                data.append(SourceTypeSerializer(source_type).data)
        else:
            for source_type in registry.get():
                data.append(SourceTypeSerializer(source_type).data)
        return Response(data)
