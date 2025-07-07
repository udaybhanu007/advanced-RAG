package com.example.springbootapp.controller;

import com.example.springbootapp.service.ConfluenceService;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.web.bind.annotation.*;
import java.util.Map;

@RestController
@RequestMapping("/api/confluence")
public class ConfluenceController {
    @Autowired
    private ConfluenceService confluenceService;

    @GetMapping("/page/{pageId}")
    public Map<String, String> getConfluencePage(@PathVariable String pageId) {
        return confluenceService.getPageContent(pageId);
    }
}
