//! Authenticated HTTP client for the Python command/state API.
use serde_json::Value;

#[derive(Clone)]
pub(super) struct Api {
    pub(super) root: String,
    pub(super) token: String,
    pub(super) agent: ureq::Agent,
}
impl Api {
    pub(super) fn call(&self, path: &str, body: Option<Value>) -> Result<Value, String> {
        let url = format!("{}{path}", self.root);
        let auth = format!("Bearer {}", self.token);
        let result = match body {
            Some(body) => self
                .agent
                .post(&url)
                .header("Authorization", &auth)
                .send_json(body),
            None => self.agent.get(&url).header("Authorization", &auth).call(),
        };
        let mut response = result.map_err(|e| e.to_string())?;
        let status = response.status();
        let payload = response
            .body_mut()
            .read_json::<Value>()
            .map_err(|e| e.to_string())?;
        if status.is_success() {
            Ok(payload)
        } else {
            Err(payload["error"]
                .as_str()
                .unwrap_or("Backend request failed")
                .to_owned())
        }
    }
}
