#!/usr/bin/env bash
set -euo pipefail
umask 077

: "${TEST_ROOT:?}" "${BASE_A:?}" "${BASE_B:?}" "${MOCK_BASE:?}"
for base in "$BASE_A" "$BASE_B" "$MOCK_BASE"; do
  [[ "$base" =~ ^http://127\.0\.0\.1:[0-9]+$ ]] || exit 1
done
export CREDENTIALS_DIRECTORY="$TEST_ROOT/credentials"
export OMNIROUTE_COOKIE_JAR=/dev/null
request() {
  curl --fail --silent --show-error --connect-timeout 5 --max-time 30 \
    --header "@$auth_file" "$@"
}
source "$(dirname "$0")/omniroute-provision.sh"
use_gateway() {
  case "$1" in
    a) export OMNIROUTE_BASE_URL="$BASE_A";;
    b) export OMNIROUTE_BASE_URL="$BASE_B";;
    *) return 1;;
  esac
  auth_file="$TEST_ROOT/$1.headers"
}

case "${1:-}" in
  apply)
    mkdir -p "$TEST_ROOT/keys-a" "$TEST_ROOT/keys-b"
    jq -n '{
      combos: {"b-group": {strategy:"priority", members:["bsmoke/test"], bridges:[]}},
      mappings: {"omniroute/b-group": {combo:"b-group", priority:100}},
      keys: {
        "B client": {fileName:"dejana-client-key"},
        "Can fallback bridge": {fileName:"dejana-bridge-key"}
      } | with_entries(.value += {
        modelAccessMode:"restricted", allowedModels:["b-group","omniroute/b-group"],
        allowedCombos:["b-group"], scopes:[], disableNonPublicModels:false, expiresAt:null
      })
    }' >"$TEST_ROOT/plan-b.json"
    jq -n '{
      combos: {"a-group": {strategy:"priority", members:[], bridges:["dejana/omniroute/b-group"]}},
      mappings: {"omniroute/a-group": {combo:"a-group", priority:100}},
      keys: {"A client": {
        fileName:"can-client-key", modelAccessMode:"restricted",
        allowedModels:["a-group","omniroute/a-group"], allowedCombos:["a-group"],
        scopes:[], disableNonPublicModels:false, expiresAt:null
      }}
    }' >"$TEST_ROOT/plan-a.json"

    use_gateway b
    node="$(upsert_provider_node 'B smoke' bsmoke openai-compatible chat "$MOCK_BASE/v1")"
    upsert_provider "$node" 'B smoke' upstream >/dev/null
    upsert_custom_model "$node" test
    apply_plan "$TEST_ROOT/plan-b.json" "$TEST_ROOT/keys-b"

    use_gateway a
    node="$(upsert_provider_node dejana-bridge dejana openai-compatible chat "$BASE_B/v1")"
    id="$(api_get "$BASE_A/api/providers" | jq -c --arg node "$node" \
      '.connections | map(select(.provider == $node and .name == "dejana-bridge"))' | matching_id)"
    bridge_key="$(<"$TEST_ROOT/keys-b/dejana-bridge-key")"
    payload="$(API_KEY="$bridge_key" jq -n --arg node "$node" \
      '{provider:$node,name:"dejana-bridge",apiKey:env.API_KEY}')"
    if [ -n "$id" ]; then
      printf '%s' "$payload" | request --request PUT --header 'Content-Type: application/json' \
        --data-binary @- "$BASE_A/api/providers/$id" >/dev/null
    else
      id="$(printf '%s' "$payload" | request --header 'Content-Type: application/json' \
        --data-binary @- "$BASE_A/api/providers" | jq -c '[.connection]' | matching_id)"
    fi
    test -n "$id"
    validate_key "$bridge_key" "$BASE_B"
    activate_bridge "$id" dejana-bridge
    api_get "$BASE_A/api/providers/$id/models?refresh=true" | jq -e '.models | type == "array"' >/dev/null
    apply_plan "$TEST_ROOT/plan-a.json" "$TEST_ROOT/keys-a"
    printf 'PASS: real provisioner applied B then A\n'
    ;;
  snapshot)
    for gateway in b a; do
      use_gateway "$gateway"
      for resource in providers provider-nodes combos model-combo-mappings keys; do
        api_get "$OMNIROUTE_BASE_URL/api/$resource" | jq -cS --arg resource "$resource" '
          (if $resource == "providers" then .connections | map({id,name,provider,proxyEnabled,isActive})
           elif $resource == "provider-nodes" then .nodes | map({id,name,prefix,baseUrl})
           elif $resource == "combos" then .combos | map({id,name,strategy,models})
           elif $resource == "model-combo-mappings" then .mappings | map({id,pattern,comboId,priority,enabled})
           else .keys | map({id,name,keyHash,modelAccessMode,allowedModels,allowedCombos,scopes,expiresAt})
           end) | sort_by(.id)'
      done
      while IFS= read -r name; do
        policy="$(jq -c --arg name "$name" '.keys[$name]' "$TEST_ROOT/plan-$gateway.json")"
        filename="$(jq -r .fileName <<<"$policy")"
        file="$TEST_ROOT/keys-$gateway/$filename"
        test "$(stat -c %a "$file")" = 600
        digest="$(key_sha256 "$(<"$file")")"
        api_get "$OMNIROUTE_BASE_URL/api/keys" | jq -e --arg name "$name" --arg digest "$digest" '
          [.keys[] | select(.name == $name)] | length == 1 and .[0].keyHash == $digest
        ' >/dev/null
        sha256sum "$file"
      done < <(jq -r '.keys | keys[]' "$TEST_ROOT/plan-$gateway.json")
    done
    ;;
  mismatch)
    use_gateway b
    policy="$(jq -c '.keys["Can fallback bridge"]' "$TEST_ROOT/plan-b.json")"
    if ensure_key 'Can fallback bridge' "$TEST_ROOT/keys-b/dejana-client-key" "$policy"; then
      printf 'FAIL: real provisioner accepted a different valid key\n' >&2
      exit 1
    fi
    printf 'PASS: real provisioner rejected swapped credential\n'
    ;;
  recovery)
    use_gateway a
    node="$(upsert_provider_node recovery recovery openai-compatible chat "$BASE_B/v1")"
    id="$(jq -n --arg node "$node" '{provider:$node,name:"recovery-probe",apiKey:"bad-key"}' \
      | request --header 'Content-Type: application/json' --data-binary @- "$BASE_A/api/providers" \
      | jq -c '[.connection]' | matching_id)"
    test -n "$id"
    if activate_bridge "$id" recovery-probe 1 0; then
      printf 'FAIL: bad credential activated\n' >&2
      exit 1
    fi
    api_get "$BASE_A/api/providers" | jq -e --arg id "$id" \
      '[.connections[] | select(.id == $id)] | length == 1 and .[0].isActive == false' >/dev/null
    API_KEY="$(<"$TEST_ROOT/keys-b/dejana-bridge-key")" jq -n '{apiKey:env.API_KEY}' \
      | request --request PUT --header 'Content-Type: application/json' --data-binary @- \
        "$BASE_A/api/providers/$id" >/dev/null
    # Simulate a quota window in this disposable instance. Only the shared
    # probe may clear it after expiry; the production helpers never write it.
    until="$(jq -nr 'now + 300 | todateiso8601')"
    jq -n --arg until "$until" '{rateLimitedUntil:$until}' \
      | request --request PUT --header 'Content-Type: application/json' --data-binary @- \
        "$BASE_A/api/providers/$id" >/dev/null
    activate_connection "$id" recovery-probe
    if activate_bridge "$id" recovery-probe 2 0; then
      printf 'FAIL: active cooldown reported ready\n' >&2
      exit 1
    fi
    api_get "$BASE_A/api/providers" | jq -e --arg id "$id" --arg until "$until" \
      '[.connections[] | select(.id == $id)] | length == 1 and .[0].rateLimitedUntil == $until' >/dev/null
    jq -n '{rateLimitedUntil:(now - 1 | todateiso8601)}' \
      | request --request PUT --header 'Content-Type: application/json' --data-binary @- \
        "$BASE_A/api/providers/$id" >/dev/null
    activate_bridge "$id" recovery-probe 2 0
    assert_bridge_ready "$id" recovery-probe
    upsert_combo a-group priority recovery/omniroute/b-group
    printf 'PASS: real readiness gate preserves cooldown and recovers after expiry\n'
    ;;
  *) exit 2;;
esac
