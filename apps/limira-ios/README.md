# Limira iOS

Native SwiftUI client for the current Limira API surface.

## Defaults

- Xcode project: `apps/limira-ios/Limira.xcodeproj`
- Scheme: `Limira`
- Bundle ID: `com.limira.ios`
- Deployment target: iOS 17.0
- Default API base URL: `https://limira-inc.com`

The app reads `LimiraAPIBaseURL` from `Info.plist`. The shared Debug scheme also
passes `-LimiraAPIBaseURL` and `https://limira-inc.com` as launch arguments so
local simulator runs use the San Francisco production server by default.

## Local State Boundary

The app does not depend on a local user database and does not copy server user
data into the repo. Local persistence is limited to:

- Keychain auth token
- Selected organization ID
- UI drafts and lightweight preferences
- Downloaded files selected by the user

Mock services are used only by unit/UI tests.

## Build And Test

```sh
xcodebuild -project apps/limira-ios/Limira.xcodeproj \
  -scheme Limira \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' \
  build

xcodebuild -project apps/limira-ios/Limira.xcodeproj \
  -scheme Limira \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' \
  test
```

## Online Smoke

Before entering credentials, verify the unauthenticated production surface:

```sh
curl -I https://limira-inc.com/limira
curl https://limira-inc.com/api/limira/auth/organizations
curl https://limira-inc.com/api/limira/auth/google/config
curl https://limira-inc.com/api/limira/auth/wechat/config
```

Then run the app in Simulator or Xcode and enter the enterprise admin password
only at runtime. Do not write passwords into source files, schemes, fixtures, or
shell history.

For an automated live UI smoke, provide credentials through the shell at runtime.
The test is skipped unless `LIMIRA_LIVE_UI_SMOKE=YES` is set.

```sh
export LIMIRA_LIVE_UI_SMOKE=YES
export LIMIRA_LIVE_IDENTIFIER="enterprise-admin-username-or-email"
read -rs LIMIRA_LIVE_PASSWORD
export LIMIRA_LIVE_PASSWORD

xcodebuild -project apps/limira-ios/Limira.xcodeproj \
  -scheme Limira \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' \
  test
```

To include a real short research task, also set:

```sh
export LIMIRA_LIVE_RUN_RESEARCH=YES
export LIMIRA_LIVE_QUERY="Limira iOS smoke test: use only public information and keep the report short."
```

Online smoke checklist:

- Select organization `builtin-limira`.
- Sign in with an enterprise admin account.
- Verify scenarios and task history load.
- Submit one short, non-sensitive research task.
- Watch task SSE updates reach a terminal status.
- Verify artifacts refresh, including report/PDF/archive availability when ready.
- If upload testing is needed, use a non-sensitive file such as
  `limira-ios-smoke.txt`; the current API does not expose a delete endpoint for
  uploaded documents.
