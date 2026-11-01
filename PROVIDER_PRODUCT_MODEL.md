# Provider Platform / Product / Profile Model

Kiro BYO separates connection/commercial identity from executable protocol code:

1. **Adapter / transport** — executable wire protocol (`anthropic-messages`,
   `xiaomi-mimo-chat`, `openai-responses`, ...).
2. **Platform** — vendor or first-party service family (Xiaomi MiMo, Alibaba
   Model Studio, OpenAI, MiniMax, ...).
3. **Product / Plan** — commercial/API entry on that platform (ordinary API,
   Token Plan, Coding Plan, Agent Plan, compatible endpoint, ...).
4. **Profile** — volatile connection/entitlement data: base URL, region, auth,
   discovery contract and an optional bundled fallback catalogue.
5. **Model identity / invocation target** — model identity used by Kiro routing
   is independent from the endpoint/deployment/inference-profile id actually
   sent on the wire.

The structured product catalogue lives in `provider_products.py`. The old
`preset_id` remains a compatibility/UI alias in 0.1.0; configured instances also
persist `platform_id`, `product_id`, `profile_id` and `entitlement`, and old
configs infer those fields at load time.

Fast-moving product metadata can be extended without Python changes through
`$KIRO_BYO_HOME/config/provider_products.override.json`. New records must bind to
an adapter transport already installed in Kiro BYO; product metadata does not
create executable adapter classes.

Model capability/catalogue metadata can likewise be augmented through
`$KIRO_BYO_HOME/config/model_catalog.override.json`. Live provider discovery is
preferred, saved catalogue state is the outage cache, bundled profile model ids
are fallback snapshots only, and manual ids are the last resort. Missing
capability facts stay unknown rather than becoming false.

Example: Xiaomi pay-as-you-go and Token Plan products share the same
`xiaomi-mimo-chat` adapter. Token Plan CN/SGP/AMS are connection profiles, not
new Python providers. The bundled Token Plan list is not an allowlist: live
catalogue results can add future chat models immediately.

See `MODEL_CATALOG_ARCHITECTURE.md` for the external-platform research baseline,
resolution precedence and lifecycle rules.
