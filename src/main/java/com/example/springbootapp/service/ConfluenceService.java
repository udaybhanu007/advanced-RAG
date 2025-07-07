package com.example.springbootapp.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.*;
import org.springframework.stereotype.Service;
import org.springframework.web.client.RestTemplate;

import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.HashMap;
import java.util.Map;

@Service
public class ConfluenceService {

    @Value("${confluence.base-url}")
    private String baseUrl;

    @Value("${confluence.api-token}")
    private String apiToken;

    @Value("${confluence.username}")
    private String username;

    public Map<String, String> getPageContent(String pageId) {
        String url = baseUrl + "/rest/api/content/" + pageId + "?expand=body.storage,title";
        RestTemplate restTemplate = new RestTemplate();

        String auth = username + ":" + apiToken;
        String encodedAuth = Base64.getEncoder().encodeToString(auth.getBytes(StandardCharsets.UTF_8));
        HttpHeaders headers = new HttpHeaders();
        headers.set("Authorization", "Basic " + encodedAuth);
        headers.set("Accept", "application/json");

        HttpEntity<String> entity = new HttpEntity<>(headers);
        ResponseEntity<String> response = restTemplate.exchange(url, HttpMethod.GET, entity, String.class);

        String title = "-";
        String content = "-";
        try {
            ObjectMapper mapper = new ObjectMapper();
            JsonNode root = mapper.readTree(response.getBody());
            title = root.path("title").asText("-");
            content = root.path("body").path("storage").path("value").asText("-");
        } catch (Exception e) {
            // Log error if needed
        }
        Map<String, String> result = new HashMap<>();
        result.put("title", title);
        result.put("content", content);
        return result;
    }
}
