# OmniRoute plan applier: idempotent upserts + explicit-failure helpers.
#
# Sourced by the thething bootstrap and by hermetic tests. Tools (curl, jq,
# sort, head, grep, tr, mktemp) resolve via PATH; the only network seam is
# `request`, which the caller defines (cookie-authenticated curl in
# production, a stub in tests).
#
# Failure contract: every helper returns nonzero on transport failure,
# invalid JSON, or schema violation (missing id). Plain `jq -r` (not -e) is
# used where empty output is legitimate (no match); `test -n` gates required
# values. Callers must propagate with `|| return 1`, never rely on set -e.
#
# Required environment: OMNIROUTE_BASE_URL, OMNIROUTE_COOKIE_JAR.
# shellcheck disable=SC2155

if ! declare -F request >/dev/null 2>&1; then
  request() {
    curl --fail --silent --show-error --connect-timeout 5 --max-time 30 --cookie "$OMNIROUTE_COOKIE_JAR" "$@"
  }
fi

# api_get <url>: fetch + prove the body is valid JSON. Empty/null is malformed here.
api_get() {
  local url="$1" body
  body="$(request "$url")" || return 1
  printf '%s' "$body" | jq -se 'length == 1 and (.[0] | type == "object")' >/dev/null || return 1
  printf '%s' "$body"
}

# A missing match differs from a matching object with a corrupt/missing ID.
# Never convert the latter into a create operation or choose among duplicates.
matching_id() {
  jq -er '
    if type != "array" then error("invalid collection")
    elif length == 0 then ""
    elif length == 1 and (.[0].id | type == "string" and test("^[A-Za-z0-9_-]+$")) then .[0].id
    else error("ambiguous or malformed identifier") end
  '
}

key_sha256() {
  printf '%s' "$1" | sha256sum | cut -d' ' -f1
}

# Probe categories are allowlisted names only. Do not print bodies or keys.
activate_connection() {
  local id="$1" name="$2" body listing class
  body="$(printf '{}' | request --header 'Content-Type: application/json' --data-binary @- \
    "${OMNIROUTE_BASE_URL}/api/providers/${id}/test")" || {
    printf 'activate %s: probe-transport\n' "$name" >&2
    return 1
  }
  class="$(printf '%s' "$body" | jq -r '
    if type != "object" then "probe-malformed"
    elif .skipped == true and ((.diagnosis.code // "") == "exclusive_lease_active") then "probe-deferred"
    elif .skipped == true and ((.diagnosis.code // "") == "unsupported") then "probe-unsupported"
    elif .skipped == true then "probe-skipped"
    elif ((.warning // "") | test("inconclusive")) then "probe-inconclusive"
    elif .valid == true then "probe-valid"
    else "probe-invalid"
    end
  ')" || class="probe-malformed"
  case "$class" in
    probe-valid|probe-unsupported) ;;
    *) printf 'activate %s: %s\n' "$name" "$class" >&2; return 1 ;;
  esac
  listing="$(api_get "${OMNIROUTE_BASE_URL}/api/providers")" || {
    printf 'activate %s: listing-transport\n' "$name" >&2
    return 1
  }
  printf '%s' "$listing" | jq -e --arg id "$id" --arg name "$name" '
    [.connections[] | select(.id == $id and .name == $name)]
    | length == 1 and .[0].isActive == true
  ' >/dev/null || {
    printf 'activate %s: inactive\n' "$name" >&2
    return 1
  }
}

assert_bridge_ready() {
  local id="$1" name="$2" listing
  listing="$(api_get "${OMNIROUTE_BASE_URL}/api/providers")" || {
    printf 'bridge %s: listing-transport\n' "$name" >&2
    return 1
  }
  printf '%s' "$listing" | jq -e --arg id "$id" --arg name "$name" '
    [.connections[] | select(.id == $id and .name == $name)]
    | length == 1 and .[0].isActive == true and .[0].proxyEnabled == false
      and .[0].testStatus == "active" and .[0].rateLimitedUntil == null
  ' >/dev/null || {
    printf 'bridge %s: cooling\n' "$name" >&2
    return 1
  }
}

activate_bridge() {
  local id="$1" name="$2" attempts="${3:-5}" delay="${4:-10}" attempt=1
  printf '%s' '{"proxyEnabled":false}' | request --request PUT --header 'Content-Type: application/json' \
    --data-binary @- "${OMNIROUTE_BASE_URL}/api/providers/${id}" >/dev/null || {
    printf 'bridge %s: proxy-transport\n' "$name" >&2
    return 1
  }
  while :; do
    if activate_connection "$id" "$name" && assert_bridge_ready "$id" "$name"; then
      return 0
    fi
    if [ "$attempt" -ge "$attempts" ]; then
      return 1
    fi
    attempt=$((attempt + 1))
    sleep "$delay"
  done
}

upsert_provider() {
  local provider="$1" name="$2" cred="$3"
  local api_key id payload response verified
  api_key="$(<"$CREDENTIALS_DIRECTORY/$cred")" || return 1
  test -n "$api_key" || return 1
  id="$(api_get "${OMNIROUTE_BASE_URL}/api/providers" \
    | jq -r --arg provider "$provider" --arg name "$name" \
      '.connections | map(select(.provider == $provider and .name == $name))' \
    | matching_id)" || return 1
  payload="$(API_KEY="$api_key" jq -n \
    --arg provider "$provider" --arg name "$name" \
    '{provider: $provider, name: $name, apiKey: env.API_KEY}')" || return 1

  if [ -n "$id" ]; then
    printf '%s' "$payload" | request --request PUT --header 'Content-Type: application/json' \
      --data-binary @- "${OMNIROUTE_BASE_URL}/api/providers/$id" >/dev/null || return 1
  else
    response="$(printf '%s' "$payload" \
      | request --header 'Content-Type: application/json' --data-binary @- \
        "${OMNIROUTE_BASE_URL}/api/providers")" || return 1
    id="$(printf '%s' "$response" | jq -c '[.connection]' | matching_id)" || return 1
    test -n "$id" || return 1
  fi

  # Explicit re-read: a suppressed mid-flight failure must not print a stale id.
  verified="$(api_get "${OMNIROUTE_BASE_URL}/api/providers" \
    | jq -r --arg id "$id" --arg name "$name" \
      '.connections | map(select(.id == $id and .name == $name))' | matching_id)" || return 1
  test -n "$verified" || return 1
  activate_connection "$id" "$name" || return 1
  # Remote discovery is opt-in in 3.8.51. Populate this connection's catalog
  # explicitly before the namespace-qualified routing preflight consumes it.
  api_get "${OMNIROUTE_BASE_URL}/api/providers/$id/models?refresh=true" \
    | jq -e '.models | type == "array"' >/dev/null || return 1
  printf '%s\n' "$id"
}

upsert_provider_node() {
  local node_name="$1" prefix="$2" type="$3" api_type="$4" base_url="$5"
  local node_payload node_id response
  node_payload="$(jq -n --arg name "$node_name" --arg prefix "$prefix" --arg type "$type" \
    --arg apiType "$api_type" --arg baseUrl "$base_url" \
    '{name: $name, prefix: $prefix, type: $type, apiType: $apiType, baseUrl: $baseUrl}')" || return 1
  node_id="$(api_get "${OMNIROUTE_BASE_URL}/api/provider-nodes" \
    | jq -c --arg prefix "$prefix" '.nodes | map(select(.prefix == $prefix))' \
    | matching_id)" || return 1
  if [ -n "$node_id" ]; then
    printf '%s' "$node_payload" | request --request PUT --header 'Content-Type: application/json' \
      --data-binary @- "${OMNIROUTE_BASE_URL}/api/provider-nodes/${node_id}" >/dev/null || return 1
  else
    response="$(printf '%s' "$node_payload" \
      | request --header 'Content-Type: application/json' --data-binary @- \
        "${OMNIROUTE_BASE_URL}/api/provider-nodes")" || return 1
    node_id="$(printf '%s' "$response" | jq -c '[.node]' | matching_id)" || return 1
    test -n "$node_id" || return 1
  fi
  printf '%s\n' "$node_id"
}

upsert_custom_model() {
  local provider="$1" model_id="$2"
  local existing payload response new_id
  existing="$(api_get "${OMNIROUTE_BASE_URL}/api/provider-models?provider=$provider" \
    | jq -r --arg model "$model_id" \
      '.models[] | select(.id == $model or .modelId == $model) | .id // empty' \
    | head -n1)" || return 1
  if [ -z "$existing" ]; then
    payload="$(jq -n --arg provider "$provider" --arg modelId "$model_id" \
      '{provider: $provider, modelId: $modelId, apiFormat: "chat-completions"}')" || return 1
    printf '%s' "$payload" \
      | request --header 'Content-Type: application/json' --data-binary @- \
        "${OMNIROUTE_BASE_URL}/api/provider-models" >/dev/null || return 1
    # Re-read instead of trusting the response shape: the model must be listed.
    existing="$(api_get "${OMNIROUTE_BASE_URL}/api/provider-models?provider=$provider" \
      | jq -r --arg model "$model_id" \
        '.models[] | select(.id == $model or .modelId == $model) | .id // empty' \
      | head -n1)" || return 1
    test -n "$existing" || return 1
  fi
}

upsert_combo() {
  local combo_name="$1" strategy="$2"
  shift 2
  local models payload listing combo_id response new_id
  models="$(printf '%s\n' "$@" | jq -R . | jq -s .)" || return 1
  payload="$(jq -n --arg name "$combo_name" --argjson models "$models" --arg strategy "$strategy" \
    '{name: $name, models: $models, strategy: $strategy}')" || return 1
  listing="$(api_get "${OMNIROUTE_BASE_URL}/api/combos")" || return 1
  combo_id="$(printf '%s' "$listing" | jq -r --arg name "$combo_name" \
    '.combos | map(select(.name == $name))' | matching_id)" || return 1
  if [ -n "$combo_id" ]; then
    printf '%s' "$payload" | request --request PUT --header 'Content-Type: application/json' \
      --data-binary @- "${OMNIROUTE_BASE_URL}/api/combos/${combo_id}" >/dev/null || return 1
  else
    response="$(printf '%s' "$payload" | request --header 'Content-Type: application/json' \
      --data-binary @- "${OMNIROUTE_BASE_URL}/api/combos")" || return 1
    new_id="$(printf '%s' "$response" | jq -c '[.]' | matching_id)" || return 1
    test -n "$new_id" || return 1
  fi
}

upsert_mapping() {
  local pattern="$1" combo_name="$2" priority="${3:-100}"
  local combos combo_id mapping_id payload response new_id
  combos="$(api_get "${OMNIROUTE_BASE_URL}/api/combos")" || return 1
  combo_id="$(printf '%s' "$combos" | jq -r --arg name "$combo_name" \
    '.combos | map(select(.name == $name))' | matching_id)" || return 1
  test -n "$combo_id" || return 1
  mapping_id="$(api_get "${OMNIROUTE_BASE_URL}/api/model-combo-mappings" \
    | jq -r --arg pattern "$pattern" \
      '.mappings | map(select(.pattern == $pattern))' | matching_id)" || return 1
  payload="$(jq -n --arg pattern "$pattern" --arg comboId "$combo_id" --argjson priority "$priority" \
    '{pattern: $pattern, comboId: $comboId, priority: $priority, enabled: true}')" || return 1
  if [ -n "$mapping_id" ]; then
    printf '%s' "$payload" | request --request PUT --header 'Content-Type: application/json' \
      --data-binary @- "${OMNIROUTE_BASE_URL}/api/model-combo-mappings/${mapping_id}" >/dev/null || return 1
  else
    response="$(printf '%s' "$payload" | request --header 'Content-Type: application/json' \
      --data-binary @- "${OMNIROUTE_BASE_URL}/api/model-combo-mappings")" || return 1
    new_id="$(printf '%s' "$response" | jq -c '[.mapping // .]' | matching_id)" || return 1
    test -n "$new_id" || return 1
  fi
}

# This checks bearer authentication, not model availability or inference.
validate_key() {
  local response status body base="${2:-$OMNIROUTE_BASE_URL}"
  case "$1" in ""|*[[:space:][:cntrl:]]*) return 1;; esac
  test -n "$base" || return 1
  response="$(printf 'Authorization: Bearer %s\n' "$1" | curl \
    --silent --show-error --connect-timeout 5 --max-time 15 \
    --write-out '\n%{http_code}' --header @- "${base}/api/v1/me/status")" || return 1
  status="${response##*$'\n'}" body="${response%$'\n'*}"
  # This route authenticates before checking self:usage. Its exact Forbidden
  # response recognizes our non-expiring operational keys without granting a
  # scope or depending on model discovery. Upstream caches authentication for
  # 60 seconds; immediate expiry enforcement is tested on chat, not this route.
  case "$status" in
    403) printf '%s' "$body" | jq -se 'length == 1 and .[0] == {error:"Forbidden"}' >/dev/null;;
    200) printf '%s' "$body" | jq -se 'length == 1 and (.[0].apiKey.id | type == "string" and length > 0)' >/dev/null;;
    *) return 1;;
  esac
}

# No automatic revocation: network errors cannot distinguish a bad key from
# an unavailable service. A lost key file requires explicit operator recovery.
ensure_key() (
  umask 077
  key_name="$1" key_file="$2" policy_json="$3"
  mode="${4:-apply}"
  tmp=""
  created=0
  trap '[ -z "$tmp" ] || rm -f "$tmp"' EXIT
  test ! -L "$key_file" || return 1
  patch="$(printf '%s' "$policy_json" | jq -esc '
    if length != 1 or (.[0] | type != "object") then error("invalid key policy") else .[0] end
    | del(.fileName)
    | if (.modelAccessMode != "restricted" and .modelAccessMode != "all")
      or (.allowedModels | type != "array") or (.allowedCombos | type != "array")
      or (.scopes | type != "array") then error("invalid key policy") else . end
  ')" || return 1
  listing="$(api_get "${OMNIROUTE_BASE_URL}/api/keys")" || return 1
  key_id="$(printf '%s' "$listing" | jq -c --arg name "$key_name" '.keys | map(select(.name == $name))' | matching_id)" || return 1
  if [ -n "$key_id" ]; then
    test -s "$key_file" || { printf 'Existing key has no local credential; explicit recovery required\n' >&2; return 1; }
    raw_key="$(<"$key_file")"
    stored_hash="$(printf '%s' "$listing" | jq -r --arg id "$key_id" '
      [.keys[] | select(.id == $id)] | if length != 1 then empty else .[0].keyHash // empty end
    ')" || return 1
    test -n "$stored_hash" || { printf 'Existing key has no digest; explicit recovery required\n' >&2; return 1; }
    test "$(key_sha256 "$raw_key")" = "$stored_hash" || { printf 'Credential does not match selected key\n' >&2; return 1; }
    validate_key "$raw_key" || return 1
    [ "$mode" != preflight ] || return 0
    printf '%s' "$patch" | request --request PATCH --header 'Content-Type: application/json' \
      --data-binary @- "${OMNIROUTE_BASE_URL}/api/keys/${key_id}" >/dev/null || return 1
  else
    # Do not replace a previously published credential when metadata disappears.
    test ! -e "$key_file" || { printf 'Credential exists without matching metadata; explicit recovery required\n' >&2; return 1; }
    # disableNonPublicModels is update-only; its default false is required for
    # atomic creation until upstream supports that field on POST too.
    payload="$(printf '%s' "$patch" | jq -ec --arg name "$key_name" '
      if (.disableNonPublicModels // false) then error("unsupported creation policy") else . end
      | del(.disableNonPublicModels) | .name = $name
    ')" || return 1
    [ "$mode" != preflight ] || return 0
    response="$(printf '%s' "$payload" | request --header 'Content-Type: application/json' \
      --data-binary @- "${OMNIROUTE_BASE_URL}/api/keys")" || return 1
    key_id="$(printf '%s' "$response" | jq -c '[.]' | matching_id)" || return 1
    raw_key="$(printf '%s' "$response" | jq -er '.key | select(type == "string" and length > 0 and length <= 4096)')" || return 1
    created=1
  fi
  # Verify persisted policy, including explicit null expiry, before publication.
  listing="$(api_get "${OMNIROUTE_BASE_URL}/api/keys")" || return 1
  if [ "$created" = 1 ]; then
    # Upstream always adds self:usage during POST. Reconcile only that scope
    # before handing the key out; expiry and model access were in the INSERT.
    printf '%s' "$listing" | jq -e --arg id "$key_id" --argjson policy "$patch" '
      [.keys[] | select(.id == $id)] | length == 1 and
      (.[0].scopes | sort) == (($policy.scopes + ["self:usage"]) | unique)
    ' >/dev/null || return 1
    printf '%s' "$patch" | jq -c '{scopes}' | request --request PATCH --header 'Content-Type: application/json' \
      --data-binary @- "${OMNIROUTE_BASE_URL}/api/keys/${key_id}" >/dev/null || return 1
    listing="$(api_get "${OMNIROUTE_BASE_URL}/api/keys")" || return 1
  fi
  printf '%s' "$listing" | jq -e --arg id "$key_id" --arg name "$key_name" --argjson policy "$patch" '
    [.keys[] | select(.id == $id and .name == $name)]
    | if length != 1 then false else .[0] as $key
      | all($policy | to_entries[]; . as $field
          | if ($field.value | type) == "array" then ($key[$field.key] | sort) == ($field.value | sort)
            elif $field.key == "disableNonPublicModels" then ($key[$field.key] // false) == $field.value
            else $key[$field.key] == $field.value end)
      end
  ' >/dev/null || return 1
  validate_key "$raw_key" || return 1
  if [ ! -e "$key_file" ]; then
    tmp="$(mktemp "${key_file}.XXXXXX")" || return 1
    printf '%s\n' "$raw_key" >"$tmp" || return 1
    chmod 600 "$tmp" && sync "$tmp" && mv -T "$tmp" "$key_file" || return 1
    tmp=""
    sync "$(dirname "$key_file")" || return 1
  else
    chmod 600 "$key_file" || return 1
  fi
)

# Validate the entire plan before any mutations. Process-substitution producer
# errors are not observable through read/mapfile, so capture jq output first.
validate_plan() {
  jq -se '
    def safe_name: type == "string" and length > 0 and (test("[[:cntrl:]]") | not);
    def labels: type == "array" and all(.[]; safe_name);
    length == 1 and (.[0] |
      type == "object" and (.combos | type == "object") and (.mappings | type == "object") and (.keys | type == "object")
      and all(.combos | to_entries[]; (.key | safe_name) and (.value | (.members | labels) and (.bridges | labels)
        and ((.members + .bridges) | length > 0) and (.strategy | safe_name)))
      and (. as $plan | all(.mappings | to_entries[]; . as $entry | (.key | safe_name) and (.value.combo | safe_name)
        and ($plan.combos | has($entry.value.combo))))
    )
  ' "$1" >/dev/null || return 1
  jq -e '
    . as $plan |
    all(.mappings[]; (.priority | type == "number" and . == floor)) and
    all(.keys | to_entries[]; (.key | type == "string" and length > 0 and (test("[[:cntrl:]]") | not))
      and (.value | (.fileName | type == "string" and test("^[A-Za-z0-9][A-Za-z0-9._-]*$"))
        and (.modelAccessMode == "restricted" or .modelAccessMode == "all")
        and (.allowedModels | type == "array" and all(.[]; type == "string" and length > 0))
        and (.allowedCombos | type == "array" and all(.[]; . as $combo | type == "string" and ($plan.combos | has($combo))))
        and (.scopes | type == "array" and all(.[]; type == "string" and length > 0))
        and ((has("expiresAt") | not) or .expiresAt == null or (.expiresAt | type == "string"))
      )) and
    ([.keys[].fileName] | length == (unique | length))
  ' "$1" >/dev/null
}

# apply_plan <plan.json> <bootstrap_dir>: preflight, combos, mappings, keys.
apply_plan() {
  local plan="$1" bootstrap_dir="$2"
  local name names members_json member_arr combo policy file_name strategy priority
  validate_plan "$plan" || return 1
  assert_plan_members "$plan" || return 1
  names="$(jq -r '.keys | keys[]' "$plan")" || return 1
  if [ -n "$names" ]; then
    while IFS= read -r name; do
      policy="$(jq -c --arg n "$name" '.keys[$n]' "$plan")" || return 1
      file_name="$(jq -r --arg n "$name" '.keys[$n].fileName' "$plan")" || return 1
      ensure_key "$name" "${bootstrap_dir}/${file_name}" "$policy" preflight || return 1
    done <<<"$names"
  fi
  names="$(jq -r '.combos | keys[]' "$plan")" || return 1
  if [ -n "$names" ]; then
  while IFS= read -r name; do
    members_json="$(jq -r --arg n "$name" '.combos[$n].members[]?, .combos[$n].bridges[]?' "$plan")" || return 1
    strategy="$(jq -r --arg n "$name" '.combos[$n].strategy' "$plan")" || return 1
    mapfile -t member_arr <<<"$members_json"
    upsert_combo "$name" "$strategy" "${member_arr[@]}" || return 1
  done <<<"$names"
  fi
  names="$(jq -r '.mappings | keys[]' "$plan")" || return 1
  if [ -n "$names" ]; then
  while IFS= read -r name; do
    combo="$(jq -r --arg n "$name" '.mappings[$n].combo // empty' "$plan")" || return 1
    priority="$(jq -r --arg n "$name" '.mappings[$n].priority' "$plan")" || return 1
    test -n "$combo" || return 1
    upsert_mapping "$name" "$combo" "$priority" || return 1
  done <<<"$names"
  fi
  names="$(jq -r '.keys | keys[]' "$plan")" || return 1
  if [ -n "$names" ]; then
  while IFS= read -r name; do
    policy="$(jq -c --arg n "$name" '.keys[$n]' "$plan")" || return 1
    file_name="$(jq -r --arg n "$name" '.keys[$n].fileName' "$plan")" || return 1
    test -n "$file_name" || return 1
    ensure_key "$name" "${bootstrap_dir}/${file_name}" "$policy" || return 1
  done <<<"$names"
  fi
}

# assert_plan_members <plan.json>: every combo member resolves inside its own
# provider namespace (head segment before the first "/" selects the provider,
# bare IDs resolve against the opencode connection). Declared bridges resolve
# against bridge nodes only — never against the model catalog.
assert_plan_members() {
  local plan="$1" connections nodes members member namespace remainder ids id models found kind node_id
  validate_plan "$plan" || return 1
  connections="$(api_get "${OMNIROUTE_BASE_URL}/api/providers")" || return 1
  nodes="$(api_get "${OMNIROUTE_BASE_URL}/api/provider-nodes")" || return 1
  printf '%s' "$connections" | jq -e '.connections | type == "array" and all(.[];
    (.id | type == "string" and test("^[A-Za-z0-9_-]+$")) and (.provider | type == "string"))' >/dev/null || return 1
  printf '%s' "$nodes" | jq -e '.nodes | type == "array" and all(.[];
    (.id | type == "string") and (.prefix | type == "string"))' >/dev/null || return 1
  for kind in members bridges; do
    members="$(jq -r --arg kind "$kind" '[.combos[] | .[$kind][]] | unique[]' "$plan")" || return 1
    [ -n "$members" ] || continue
    while IFS= read -r member; do
      namespace="${member%%/*}" remainder="${member#*/}"
      if [[ "$member" != */* ]]; then namespace=opencode; remainder="$member"; fi
      node_id="$(printf '%s' "$nodes" | jq -er --arg prefix "$namespace" '
        [.nodes[] | select(.prefix == $prefix)]
        | if length == 0 then "" elif length == 1 then .[0].id else error("ambiguous prefix") end
      ')" || return 1
      if [ "$kind" = bridges ]; then
        [ -n "$node_id" ] && [[ "$remainder" == omniroute/?* ]] || return 1
      fi
      ids="$(printf '%s' "$connections" | jq -r --arg provider "${node_id:-$namespace}" '
        .connections[] | select(.provider == $provider and .isActive != false) | .id
      ')" || return 1
      found=0
      if [ -n "$ids" ]; then
        while IFS= read -r id; do
          # Query only this member namespace. An unused optional provider is
          # not a preflight dependency, and accounts cannot overwrite each other.
          models="$(api_get "${OMNIROUTE_BASE_URL}/api/providers/${id}/models")" || continue
          if printf '%s' "$models" | jq -e --arg full "$member" --arg short "$remainder" --arg kind "$kind" '
            (.models | type == "array") and any(.models[]; (.id // .model) as $id
              | $id == $full or $id == $short or ($kind == "bridges" and $id == ($short | ltrimstr("omniroute/"))))
          ' >/dev/null; then found=1; break; fi
        done <<<"$ids"
      fi
      if [ "$found" = 0 ]; then printf 'Unresolved managed %s: %s\n' "$kind" "$member" >&2; return 1; fi
    done <<<"$members"
  done
}
