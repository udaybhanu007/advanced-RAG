package com.example.springbootapp.controller;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import com.example.springbootapp.model.ExampleModel;
import com.example.springbootapp.service.ExampleService;

import java.util.List;

@RestController
@RequestMapping("/api/examples")
public class ExampleController {

    @Autowired
    private ExampleService exampleService;

    // Create
    @PostMapping
    public ResponseEntity<ExampleModel> createExample(@RequestBody ExampleModel exampleModel) {
        try {
            return ResponseEntity.ok(exampleService.saveExample(exampleModel));
        } catch (Exception e) {
            return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR).body(null);
        }
    }

    // Read All
    @GetMapping
    public ResponseEntity<List<ExampleModel>> getAllExamples() {
        try {
            return ResponseEntity.ok(exampleService.getAllExamples());
        } catch (Exception e) {
            return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR).body(null);
        }
    }

    // Read by ID
    @GetMapping("/{id}")
    public ResponseEntity<ExampleModel> getExampleById(@PathVariable Long id) {
        try {
            return ResponseEntity.ok(exampleService.getExampleById(id));
        } catch (Exception e) {
            return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR).body(null);
        }
    }

    // Update
    @PutMapping("/{id}")
    public ResponseEntity<ExampleModel> updateExample(@PathVariable Long id, @RequestBody ExampleModel exampleModel) {
        try {
            return ResponseEntity.ok(exampleService.updateExample(id, exampleModel));
        } catch (Exception e) {
            return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR).body(null);
        }
    }

    // Delete
    @DeleteMapping("/{id}")
    public ResponseEntity<Void> deleteExample(@PathVariable Long id) {
        try {
            exampleService.deleteExample(id);
            return ResponseEntity.noContent().build();
        } catch (Exception e) {
            return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR).build();
        }
    }
}
