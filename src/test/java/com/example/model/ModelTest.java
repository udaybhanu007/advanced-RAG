package com.example.model;

import com.example.springbootapp.model.ExampleModel;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;

class ModelTest {
    @Test
    void testModelFields() {
        ExampleModel model = new ExampleModel();
        model.setGender("Female");
        assertEquals("Female", model.getGender());
        // No salary field to test
    }
}